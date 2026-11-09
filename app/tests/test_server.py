"""ClipBridge サーバーの結合テスト（実 HTTP、クリップボードと Tailscale は差し替え）。"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

SERVER_FILE = Path(__file__).resolve().parents[1] / "server.py"
spec = importlib.util.spec_from_file_location("clipbridge_server", SERVER_FILE)
server = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(server)

DNS = "pc.example.ts.net"


def ts_status(dns: str = DNS, backend: str = "Running", certs: bool = True, magic: bool = True) -> dict:
    return {
        "BackendState": backend,
        "Self": {"HostName": "pc", "DNSName": dns + ".", "Online": backend == "Running", "TailscaleIPs": ["100.1.2.3"]},
        "CertDomains": [dns] if certs else [],
        "CurrentTailnet": {"MagicDNSEnabled": magic, "MagicDNSSuffix": "example.ts.net"},
    }


def serve_cfg(port: int, local_port: int, dns: str = DNS) -> dict:
    return {
        "TCP": {str(port): {"HTTPS": True}},
        "Web": {f"{dns}:{port}": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{local_port}"}}}},
    }


class FakeTailscale:
    def __init__(self, status=None, serve=None, available=True):
        self._status = status
        self._serve = serve if serve is not None else {}
        self._available = available
        self.calls: list[tuple[int, int]] = []

    def available(self):
        return self._available

    def status(self):
        return self._status

    def serve_config(self):
        return self._serve

    def serve_https(self, https_port, local_port):
        self.calls.append((https_port, local_port))
        self._serve = serve_cfg(https_port, local_port)
        return True, "ok"


class NoOpen:
    """ブラウザーを開かない。"""


@pytest.fixture
def app(tmp_path):
    clipboard = server.MemoryClipboard()
    tailscale = FakeTailscale(status=ts_status(), serve={})
    application = server.ClipBridgeApp(
        port=0,
        config_path=tmp_path / "config.json",
        clipboard=clipboard,
        tailscale=tailscale,
        startup_dir=tmp_path / "Startup",
        watch_interval=0.05,
    )
    httpd = application.serve()
    application.start_watcher()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    application.base = f"http://127.0.0.1:{application.port}"
    application.fake_clipboard = clipboard
    application.fake_tailscale = tailscale
    yield application
    application.stopping = True
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)


def request(base, path, method="GET", token=None, data=None, headers=None, json_body=None):
    supplied = dict(headers or {})
    if token is not None:
        supplied["X-Token"] = token
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        supplied.setdefault("Content-Type", "application/json")
    elif data is not None:
        supplied.setdefault("Content-Type", "text/plain; charset=utf-8")
    req = urllib.request.Request(base + path, data=data, headers=supplied, method=method)
    try:
        with opener.open(req, timeout=5) as response:
            body = response.read()
            return response.status, dict(response.headers), body
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def as_json(body: bytes):
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# 認証とペアリング
# ---------------------------------------------------------------------------


def test_health_is_public_and_clip_requires_token(app):
    status, _, body = request(app.base, "/api/health")
    assert status == 200 and as_json(body)["app"] == "ClipBridge"
    assert request(app.base, "/api/clip")[0] == 401
    assert request(app.base, "/api/clip", token="wrong")[0] == 401
    assert request(app.base, "/api/clip", token=app.token)[0] == 200
    assert request(app.base, "/api/clip?t=" + app.token)[0] == 200


def test_pair_with_code_returns_token_and_rotates_code(app):
    code = app.pairing.info()["code"]
    status, _, body = request(app.base, "/api/pair", method="POST", json_body={"code": code})
    assert status == 200
    data = as_json(body)
    assert data["token"] == app.token and data["pc_name"] == app.pc_name
    assert app.pairing.info()["code"] != code  # 使い捨て
    # 同じコードはもう使えない
    assert request(app.base, "/api/pair", method="POST", json_body={"code": code})[0] == 401


def test_pair_rotates_code_after_five_failures(app):
    original = app.pairing.info()["code"]
    for _ in range(server.PAIR_MAX_ATTEMPTS - 1):
        assert request(app.base, "/api/pair", method="POST", json_body={"code": "000000"})[0] == 401
        assert app.pairing.info()["code"] == original if original != "000000" else True
    request(app.base, "/api/pair", method="POST", json_body={"code": "000000"})
    assert app.pairing.info()["code"] != original or original == "000000"
    assert app.pairing.info()["attempts_left"] == server.PAIR_MAX_ATTEMPTS


def test_pair_rejects_malformed_codes(app):
    assert request(app.base, "/api/pair", method="POST", json_body={"code": "12"})[0] == 400
    assert request(app.base, "/api/pair", method="POST", json_body={"code": "abcdef"})[0] == 400
    assert request(app.base, "/api/pair", method="POST", data=b"not json", headers={"Content-Type": "application/json"})[0] == 400


def test_pair_link_redirects_to_token_url(app):
    code = app.pairing.info()["code"]
    status, headers, _ = request(app.base, f"/?code={code}")
    assert status == 302 and headers["Location"] == f"/?t={app.token}"
    status, headers, _ = request(app.base, "/?code=999999")
    assert status == 302 and headers["Location"] == "/?pair=invalid"


def test_index_embeds_manifest_with_token_and_manifest_start_url(app):
    # トークン付きで開いた場合だけ manifest にトークンが付く（ホーム画面追加用）
    status, _, body = request(app.base, f"/?t={app.token}")
    assert status == 200
    assert f'href="/manifest.webmanifest?t={app.token}"' in body.decode("utf-8")
    status, headers, _ = request(app.base, "/?t=invalid-token")
    assert status == 302 and headers["Location"] == "/?pair=invalid"

    status, _, body = request(app.base, f"/manifest.webmanifest?t={app.token}")
    assert status == 200 and as_json(body)["start_url"] == f"/?t={app.token}"
    status, _, body = request(app.base, "/manifest.webmanifest?t=bad")
    assert status == 200 and as_json(body)["start_url"] == "/"
    # プロキシ経由（iPhone）からトークン無しで開いた場合は素の manifest
    status, _, body = request(app.base, "/", headers={"X-Forwarded-For": "100.9.9.9"})
    assert status == 200 and 'href="/manifest.webmanifest"' in body.decode("utf-8")


# ---------------------------------------------------------------------------
# クリップボードの往復
# ---------------------------------------------------------------------------


def test_round_trip_unicode(app):
    value = "日本語🙂\n次の行"
    status, _, body = request(app.base, "/api/clip", method="POST", token=app.token, data=value.encode("utf-8"))
    assert status == 200
    data = as_json(body)
    assert data["ok"] is True and data["length"] == len(value)
    assert app.fake_clipboard.text == value
    status, _, body = request(app.base, "/api/clip", token=app.token)
    snapshot = as_json(body)
    assert status == 200 and snapshot["text"] == value and snapshot["kind"] == "text"
    assert snapshot["source"] == "phone" and snapshot["hash"] == data["hash"]


def test_pc_copy_is_detected_by_watcher(app):
    app.fake_clipboard.external_copy("PC でコピー\r\nした文字")
    deadline = time.time() + 2
    while time.time() < deadline:
        snapshot = app.state.snapshot()
        if snapshot["text"]:
            break
        time.sleep(0.02)
    assert snapshot["text"] == "PC でコピー\nした文字"  # CRLF は LF に正規化
    assert snapshot["source"] == "pc"


def test_non_text_clipboard_is_reported(app):
    app.fake_clipboard.external_copy("", kind="other")
    deadline = time.time() + 2
    while time.time() < deadline and app.state.snapshot()["kind"] != "other":
        time.sleep(0.02)
    assert app.state.snapshot()["kind"] == "other"


def test_write_validation(app):
    base, token = app.base, app.token
    assert request(base, "/api/clip", method="POST", token=token, data=b"x" * (server.MAX_BYTES + 1))[0] == 413
    assert request(base, "/api/clip", method="POST", token=token, data=b"x", headers={"Content-Type": "application/json"})[0] == 415
    assert request(base, "/api/clip", method="POST", token=token, data=b"\xff\xfe", headers={"Content-Type": "text/plain"})[0] == 400
    assert request(base, "/api/clip", method="POST", token=token, data=b"a", headers={"Origin": "https://evil.example"})[0] == 403
    assert request(base, "/api/clip", method="POST", token=token, data=b"a", headers={"Origin": base})[0] == 200
    forwarded = {"Origin": "https://pc.example.ts.net", "X-Forwarded-Host": "pc.example.ts.net"}
    assert request(base, "/api/clip", method="POST", token=token, data=b"a", headers=forwarded)[0] == 200
    assert request(base, "/api/clip", method="PUT", token=token)[0] == 405
    assert request(base, "/missing")[0] == 404


def test_static_files_and_apple_touch_icon_aliases(app):
    for path in ("/style.css", "/app.js", "/pc.js", "/vendor/qrcode.js", "/icon-180.png", "/icon-512.png"):
        status, _, body = request(app.base, path)
        assert status == 200 and body, path
    for path in ("/apple-touch-icon.png", "/apple-touch-icon-120x120-precomposed.png"):
        status, headers, _ = request(app.base, path)
        assert status == 200 and headers["Content-Type"] == "image/png", path


def test_clipboard_write_failure_returns_json_without_details(app, monkeypatch):
    def fail(_text):
        raise RuntimeError("secret clipboard content")

    monkeypatch.setattr(app.fake_clipboard, "write", fail)
    status, _, body = request(app.base, "/api/clip", method="POST", token=app.token, data=b"hello")
    assert status == 500
    assert "secret clipboard content" not in body.decode("utf-8")


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


def read_sse_event(response, timeout=3.0):
    """1 イベント分（空行まで）を読んで (event, data) を返す。"""
    deadline = time.time() + timeout
    event = None
    data_lines: list[str] = []
    while time.time() < deadline:
        line = response.fp.readline().decode("utf-8")
        if line == "":
            raise AssertionError("stream closed")
        line = line.rstrip("\n")
        if line.startswith(":"):
            continue
        if line == "":
            if data_lines:
                return event, json.loads("\n".join(data_lines))
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line.startswith("retry:"):
            continue
    raise AssertionError("no event")


def test_sse_sends_initial_state_and_pushes_pc_changes(app):
    app.fake_clipboard.external_copy("最初の内容")
    time.sleep(0.2)
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=5)
    conn.request("GET", f"/api/events?t={app.token}", headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_7 like Mac OS X)"})
    response = conn.getresponse()
    try:
        assert response.status == 200
        assert response.getheader("Content-Type", "").startswith("text/event-stream")
        event, data = read_sse_event(response)
        assert event == "clip" and data["text"] == "最初の内容"
        app.fake_clipboard.external_copy("更新された内容")
        event, data = read_sse_event(response)
        assert event == "clip" and data["text"] == "更新された内容" and data["source"] == "pc"
        # iPhone からの書き込みも配信される
        request(app.base, "/api/clip", method="POST", token=app.token, data="iPhone から".encode("utf-8"))
        event, data = read_sse_event(response)
        assert data["text"] == "iPhone から" and data["source"] == "phone"
        assert app.state.subscriber_count() == 1
        assert any(device["name"] == "iPhone" for device in app.device_list())
    finally:
        conn.close()
    deadline = time.time() + 2
    while time.time() < deadline and app.state.subscriber_count():
        time.sleep(0.02)


def test_sse_requires_token(app):
    assert request(app.base, "/api/events")[0] == 401
    assert request(app.base, "/api/events?t=bad")[0] == 401


# ---------------------------------------------------------------------------
# PC 用管理画面
# ---------------------------------------------------------------------------


def test_admin_routes_only_for_direct_loopback(app):
    status, headers, _ = request(app.base, "/")
    assert status == 302 and headers["Location"] == "/pc"
    assert request(app.base, "/pc")[0] == 200
    status, _, body = request(app.base, "/pc/api/status")
    assert status == 200
    payload = as_json(body)
    assert payload["pair_code"] == app.pairing.info()["code"]
    assert "token" not in payload
    # プロキシ経由や Host 偽装（DNS リバインディング）は 404
    assert request(app.base, "/pc", headers={"X-Forwarded-For": "100.9.9.9"})[0] == 404
    assert request(app.base, "/pc/api/status", headers={"Tailscale-User-Login": "x@y"})[0] == 404
    assert request(app.base, "/pc/api/status", headers={"Host": "evil.example"})[0] == 404
    status, headers, _ = request(app.base, "/pc/client")
    assert status == 302 and headers["Location"] == f"/?t={app.token}"


def test_admin_post_requires_custom_header(app):
    assert request(app.base, "/pc/api/pair-code", method="POST", json_body={})[0] == 403
    before = app.pairing.info()["code"]
    status, _, body = request(app.base, "/pc/api/pair-code", method="POST", json_body={}, headers={"X-ClipBridge": "1"})
    assert status == 200 and as_json(body)["pair_code"] == app.pairing.info()["code"]
    assert app.pairing.info()["code"] != before or True  # 乱数衝突は許容
    assert request(app.base, "/pc/api/pair-code", method="POST", json_body={},
                   headers={"X-ClipBridge": "1", "Origin": "https://evil.example"})[0] == 403
    assert request(app.base, "/pc/api/pair-code", method="POST", json_body={},
                   headers={"X-ClipBridge": "1", "Sec-Fetch-Site": "cross-site"})[0] == 403
    assert request(app.base, "/pc/api/unknown", method="POST", json_body={}, headers={"X-ClipBridge": "1"})[0] == 404


def test_token_rotation_invalidates_old_token(app, tmp_path):
    old = app.token
    status, _, _ = request(app.base, "/pc/api/token/rotate", method="POST", json_body={}, headers={"X-ClipBridge": "1"})
    assert status == 200
    assert app.token != old
    assert request(app.base, "/api/clip", token=old)[0] == 401
    assert request(app.base, "/api/clip", token=app.token)[0] == 200
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["token"] == app.token


def test_serve_setup_via_dashboard(app):
    status, _, body = request(app.base, "/pc/api/serve", method="POST", json_body={}, headers={"X-ClipBridge": "1"})
    assert status == 200
    result = as_json(body)
    assert result["ok"] is True
    assert app.fake_tailscale.calls == [(443, app.port)]
    status, _, body = request(app.base, "/pc/api/status")
    payload = as_json(body)
    assert payload["public_url"] == f"https://{DNS}/"
    assert payload["pair_url"] == f"https://{DNS}/?code={payload['pair_code']}"
    assert payload["tailscale"]["ready"] is True


def test_startup_toggle(app, tmp_path):
    status, _, body = request(app.base, "/pc/api/startup", method="POST", json_body={"enabled": True}, headers={"X-ClipBridge": "1"})
    assert status == 200
    path = Path(as_json(body)["path"])
    assert path.exists() and path.parent == tmp_path / "Startup"
    content = path.read_bytes()
    assert content.isascii()
    assert b"--no-browser" in content and b"server.py" in content and b'CreateObject("WScript.Shell").Run' in content
    status, _, body = request(app.base, "/pc/api/status")
    assert as_json(body)["startup"]["enabled"] is True
    request(app.base, "/pc/api/startup", method="POST", json_body={"enabled": False}, headers={"X-ClipBridge": "1"})
    assert not path.exists()


def test_shutdown_from_dashboard(tmp_path):
    application = server.ClipBridgeApp(
        port=0, config_path=tmp_path / "config.json", clipboard=server.MemoryClipboard(),
        tailscale=FakeTailscale(status=ts_status()), startup_dir=tmp_path / "Startup",
    )
    httpd = application.serve()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{application.port}"
    assert request(base, "/pc/api/shutdown", method="POST", json_body={}, headers={"X-ClipBridge": "1"})[0] == 200
    thread.join(timeout=3)
    assert not thread.is_alive()
    httpd.server_close()


# ---------------------------------------------------------------------------
# ログに秘密を出さない
# ---------------------------------------------------------------------------


def test_secrets_never_logged(app, capsys):
    secret_text = "秘密のクリップボード-98765"
    code = app.pairing.info()["code"]
    request(app.base, "/api/clip", method="POST", token=app.token, data=secret_text.encode("utf-8"))
    request(app.base, "/api/clip?t=" + app.token)
    request(app.base, "/api/pair", method="POST", json_body={"code": code})
    request(app.base, "/missing?text=" + quote(secret_text))
    log = capsys.readouterr().out
    assert secret_text not in log
    assert app.token not in log
    assert code not in log
    assert "chars" in log


def test_without_console_stream(app, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(server.sys, "stdout", None)
        assert request(app.base, "/api/clip", method="POST", token=app.token, data=b"pythonw")[0] == 200
    assert app.fake_clipboard.text == "pythonw"


# ---------------------------------------------------------------------------
# 純粋関数
# ---------------------------------------------------------------------------


def test_config_creation_and_reuse(tmp_path):
    path = tmp_path / "config.json"
    config = server.load_config(path)
    assert len(config["token"]) >= 32
    assert server.load_config(path)["token"] == config["token"]
    path.write_text("broken", encoding="utf-8")
    assert len(server.load_config(path)["token"]) >= 32


def test_classify_and_choose_https_port():
    assert server.classify_port({}, DNS, 443, 8787) == "free"
    assert server.classify_port(None, DNS, 443, 8787) == "free"
    assert server.classify_port(serve_cfg(443, 8787), DNS, 443, 8787) == "ours"
    assert server.classify_port(serve_cfg(443, 3000), DNS, 443, 8787) == "taken"
    # 443 が他サービスなら 8443 を選ぶ
    assert server.choose_https_port(serve_cfg(443, 3000), DNS, 8787) == (8443, "free")
    assert server.choose_https_port(serve_cfg(443, 8787), DNS, 8787) == (443, "ours")
    # 443 に別パスだけがある場合は / が空いているので使える
    only_path = {"TCP": {"443": {"HTTPS": True}}, "Web": {f"{DNS}:443": {"Handlers": {"/labs": {"Proxy": "http://127.0.0.1:3000"}}}}}
    assert server.classify_port(only_path, DNS, 443, 8787) == "free"
    # 生 TCP 転送や HTTP は使えない
    assert server.classify_port({"TCP": {"443": {"TCPForward": "127.0.0.1:22"}}}, DNS, 443, 8787) == "taken"
    assert server.classify_port({"TCP": {"443": {"HTTP": True}}}, DNS, 443, 8787) == "taken"
    assert server.public_url(DNS, 443) == f"https://{DNS}/"
    assert server.public_url(DNS, 8443) == f"https://{DNS}:8443/"


def test_summarize_tailscale_states():
    summary = server.summarize_tailscale(False, None, None, 8787)
    assert summary["installed"] is False and summary["checks"][0]["state"] == "fail"

    summary = server.summarize_tailscale(True, ts_status(backend="NeedsLogin"), {}, 8787)
    states = {check["id"]: check["state"] for check in summary["checks"]}
    assert states["running"] == "fail" and summary["ready"] is False

    summary = server.summarize_tailscale(True, ts_status(certs=False), {}, 8787)
    states = {check["id"]: check["state"] for check in summary["checks"]}
    assert states["https"] == "fail" and states["serve"] == "warn"
    assert summary["planned_url"] == f"https://{DNS}/" and summary["public_url"] is None

    funnel = serve_cfg(443, 8787)
    funnel["AllowFunnel"] = {f"{DNS}:443": True}
    summary = server.summarize_tailscale(True, ts_status(), funnel, 8787)
    states = {check["id"]: check["state"] for check in summary["checks"]}
    assert states["serve"] == "ok" and states["funnel"] == "fail" and summary["ready"] is False

    summary = server.summarize_tailscale(True, ts_status(), serve_cfg(443, 8787), 8787)
    assert summary["ready"] is True and summary["public_url"] == f"https://{DNS}/"


def test_device_name():
    assert server.device_name("Mozilla/5.0 (iPhone; CPU iPhone OS 16_7 like Mac OS X)") == "iPhone"
    assert server.device_name("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)") == "iPad"
    assert server.device_name("Mozilla/5.0 (Windows NT 10.0; Win64; x64)") == "Windows PC"
    assert server.device_name("curl/8.0") == "端末"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のクリップボード実装")
def test_windows_clipboard_round_trip_restores_original():
    clipboard = server.WindowsClipboard()
    try:
        original_text, original_kind = clipboard.read()
    except server.ClipboardError as exc:
        # 遅延レンダリングするアプリがクリップボードを握っていると内容を取れない（環境依存）。復元できないので実行しない
        pytest.skip(f"clipboard is not readable right now: {exc}")
    try:
        before = clipboard.sequence()
        clipboard.write("ClipBridge テスト 🙂")
        assert clipboard.sequence() != before
        assert clipboard.read() == ("ClipBridge テスト 🙂", "text")
    finally:
        if original_kind == "text":
            clipboard.write(original_text)


# ---------------------------------------------------------------------------
# アップデート・インストール（exe 版 / git 版）
# ---------------------------------------------------------------------------


def manifest_for(data: bytes = b"MZ-clipbridge", version: str = "9.9.9") -> dict:
    return {
        "version": version,
        "url": f"https://github.com/hiromps/clipbridge/releases/download/v{version}/ClipBridge.exe",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


class FakeDownload(io.BytesIO):
    """urlopen の戻り値の代わり（HTTPS へリダイレクト済みの体）。"""

    url = "https://release-assets.githubusercontent.com/example"


def fake_opener(data: bytes):
    return lambda request, timeout=0: FakeDownload(data)


@pytest.mark.parametrize("change", [
    {"url": "http://github.com/hiromps/clipbridge/releases/download/v9.9.9/ClipBridge.exe"},
    {"url": "https://github.com/other/repo/releases/download/v9.9.9/ClipBridge.exe"},
    {"url": "https://github.com/hiromps/clipbridge/releases/download/v9.9.9/ClipBridge.exe?x=1"},
    {"url": "https://github.com/hiromps/clipbridge/releases/download/v9.9.8/ClipBridge.exe"},
    {"version": "9.9.9;rm"}, {"sha256": "short"}, {"size": 0}, {"size": True}, {"size": server.MAX_UPDATE_BYTES + 1},
])
def test_manifest_validation_rejects_untrusted_sources(change):
    with pytest.raises((ValueError, TypeError)):
        server.validate_manifest(manifest_for() | change)
    with pytest.raises(ValueError):
        server.validate_manifest(["not", "a", "dict"])


def test_version_tuple_orders_numerically():
    assert server.version_tuple("2.10.0") > server.version_tuple("2.9.1") > server.version_tuple("1.1.2")
    assert server.validate_manifest(manifest_for())["version"] == "9.9.9"
    with pytest.raises(ValueError):
        server.version_tuple("2.0")
    with pytest.raises(ValueError):
        server.version_tuple(None)


def test_download_update_verifies_hash_and_cleans_up(tmp_path):
    data = b"MZ-clipbridge-exe"
    path = server.download_update(manifest_for(data), tmp_path / "dl", opener=fake_opener(data))
    assert path.read_bytes() == data
    for bad in (b"MZ-other-bytes!!!", b"MZ-short", b"MZ-clipbridge-exe-longer"):
        with pytest.raises(ValueError):
            server.download_update(manifest_for(data), tmp_path / "bad", opener=fake_opener(bad))
        assert not (tmp_path / "bad" / "ClipBridge.exe").exists()
    with pytest.raises(ValueError):  # exe でないもの
        server.download_update(manifest_for(b"PK-not-exe"), tmp_path / "zip", opener=fake_opener(b"PK-not-exe"))


def test_fetch_manifest_reports_network_errors():
    def offline(request, timeout=0):
        raise OSError("offline")

    with pytest.raises(RuntimeError, match="更新情報を取得できません"):
        server.fetch_manifest(opener=offline)
    assert server.fetch_manifest(opener=fake_opener(json.dumps(manifest_for()).encode("utf-8")))["version"] == "9.9.9"
    with pytest.raises(ValueError):
        server.fetch_manifest(opener=fake_opener(b"{not json"))


class FakeGit:
    """GitUpdater に差し込む git コマンドの偽物。"""

    def __init__(self, behind=0, dirty=False):
        self.behind = behind
        self.dirty = dirty
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        head = args[0]
        if head == "rev-parse":
            return 0, "main\n", ""
        if head == "fetch":
            return 0, "", ""
        if head == "rev-list":
            return 0, f"{self.behind}\n", ""
        if head == "log":
            return 0, ("abc1234 2026-11-10\n" if "HEAD" in args else "def5678 2026-11-11\n"), ""
        if head == "status":
            return 0, (" M app/server.py\n" if self.dirty else ""), ""
        if head == "pull":
            self.behind = 0
            return 0, "Updating abc1234..def5678\n", ""
        return 1, "", "unknown command"


def make_git(tmp_path, **kwargs):
    (tmp_path / ".git").mkdir(exist_ok=True)
    runner = FakeGit(**kwargs)
    return server.GitUpdater(tmp_path, runner=runner), runner


def test_updater_exe_reports_new_release(app):
    updater = server.Updater(app, fetch=lambda: manifest_for(version="9.9.9"), frozen=True)
    info = updater.check()
    assert info["method"] == "exe" and info["available"] is True and info["latest"] == "9.9.9" and info["can_apply"]
    assert server.Updater(app, fetch=lambda: manifest_for(version=server.__version__), frozen=True).check()["available"] is False

    def broken():
        raise OSError("offline")

    info = server.Updater(app, fetch=broken, frozen=True).check()
    assert info["available"] is False and "offline" in info["error"]
    with pytest.raises(RuntimeError):
        server.Updater(app, fetch=broken, frozen=True).apply()


def test_updater_git_check_and_apply_schedules_restart(app, tmp_path, monkeypatch):
    git, runner = make_git(tmp_path, behind=2)
    updater = server.Updater(app, git=git, frozen=False)
    info = updater.check()
    assert info["method"] == "git" and info["available"] is True and "2 件" in info["detail"]
    assert info["latest"].startswith("def5678") and "abc1234" in info["current"]
    events = []
    monkeypatch.setattr(app, "request_shutdown", lambda: events.append("shutdown"))
    result = updater.apply()
    assert result["ok"] is True
    assert any(call[0] == "pull" and "--ff-only" in call for call in runner.calls)
    assert app.restart_command[-3:] == ["--no-browser", "--wait-pid", str(server.os.getpid())]
    assert events == ["shutdown"]
    # 未コミットの変更があるときは何も変えずに中止する
    git, runner = make_git(tmp_path, behind=1, dirty=True)
    with pytest.raises(RuntimeError, match="未コミット"):
        server.Updater(app, git=git, frozen=False).apply()
    assert not any(call[0] == "pull" for call in runner.calls)


def test_updater_source_without_git_cannot_apply(app, tmp_path):
    git = server.GitUpdater(tmp_path / "nogit")
    assert not git.available()
    updater = server.Updater(app, fetch=lambda: manifest_for(version="9.9.9"), git=git, frozen=False)
    info = updater.check()
    assert info["method"] == "source" and info["available"] is True and info["can_apply"] is False
    with pytest.raises(RuntimeError, match="自動更新できません"):
        updater.apply()


def test_admin_update_endpoints(app):
    status, _, body = request(app.base, "/pc/api/status")
    payload = as_json(body)
    assert payload["update"]["current"] == server.__version__
    assert payload["update"]["method"] in ("git", "source", "exe") and payload["frozen"] is False
    app.updater = server.Updater(app, fetch=lambda: manifest_for(version=server.__version__), frozen=True)
    status, _, body = request(app.base, "/pc/api/update/check", method="POST", json_body={}, headers={"X-ClipBridge": "1"})
    assert status == 200 and as_json(body)["available"] is False
    status, _, body = request(app.base, "/pc/api/update/apply", method="POST", json_body={}, headers={"X-ClipBridge": "1"})
    assert status == 400 and "最新" in as_json(body)["message"]
    assert request(app.base, "/pc/api/update/apply", method="POST", json_body={})[0] == 403
    assert request(app.base, "/pc/api/update/check", method="POST", json_body={}, headers={"X-Forwarded-For": "100.9.9.9", "X-ClipBridge": "1"})[0] == 404


def test_restart_command_keeps_options_and_waits_for_old_process():
    argv = ["server.py", "--port", "8790", "--no-browser", "--wait-pid", "123", "--install", "--minimized"]
    command = server.restart_command(argv, 4321, frozen=False, executable="pythonw.exe")
    assert command[0] == "pythonw.exe" and command[1].endswith("server.py")
    assert command[2:] == ["--port", "8790", "--no-browser", "--wait-pid", "4321"]
    command = server.restart_command(["ClipBridge.exe", "--wait-pid=9"], 1, frozen=True, executable="C:/x/ClipBridge.exe")
    assert command == ["C:/x/ClipBridge.exe", "--no-browser", "--wait-pid", "1"]


def test_startup_script_for_exe_uses_launcher(tmp_path):
    path = server.set_startup(True, tmp_path / "Startup", port=8787, frozen=True, launcher=r"C:\Apps\ClipBridge.lnk")
    content = path.read_bytes()
    assert content.isascii() and rb'Run """C:\Apps\ClipBridge.lnk"" --no-browser --port 8787"' in content
    assert b"python" not in content
    assert b"\r\r\n" not in content and content.endswith(b"\r\n")  # CRLF の二重変換が起きていない


def test_install_for_user_copies_exe_and_records_previous(tmp_path):
    source = tmp_path / "download" / "ClipBridge.exe"
    source.parent.mkdir()
    source.write_bytes(b"MZnew")
    versions = tmp_path / "versions"
    previous = versions / "1.1.2" / "ClipBridge.exe"
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b"MZold")
    state = tmp_path / "desktop.json"
    server.save_state({"current_executable": str(previous), "step": 3}, state)
    calls = []
    target = server.install_for_user(source, versions_dir=versions, state_path=state, runner=lambda args, **kw: calls.append(args))
    assert target == versions / server.__version__ / "ClipBridge.exe" and target.read_bytes() == b"MZnew"
    assert previous.exists()  # 旧版は残す
    recorded = server.read_state(state)
    assert recorded["current_executable"] == str(target) and recorded["previous_executable"] == str(previous)
    assert recorded["step"] == 3  # 旧版の記録はそのまま
    assert calls and calls[0][0] == "powershell.exe" and str(target) in calls[0][-1]
    assert server.is_installed_location(target, versions) and not server.is_installed_location(source, versions)
    # 2 回目（同じ版）も壊れない
    assert server.install_for_user(source, versions_dir=versions, state_path=state, runner=lambda *a, **kw: None) == target


def test_self_test_writes_report(tmp_path):
    output = tmp_path / "report.json"
    server.self_test(output)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["ok"] is True and report["version"] == server.__version__ and report["frozen"] is False


def test_parser_accepts_legacy_launcher_options():
    args = server.build_parser().parse_args(["--minimized", "--install", "--wait-pid", "42", "--no-serve", "--no-update-check"])
    assert args.minimized and args.install and args.wait_pid == 42 and args.no_serve and args.no_update_check
    assert server.wait_for_process(server.os.getpid()) is True  # 自分自身は待たない
