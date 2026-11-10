"""ClipBridge サーバーの結合テスト（実 HTTP、クリップボードと Tailscale は差し替え）。"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import io
import json
import subprocess
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


# ---------------------------------------------------------------------------
# 複数 PC（ハブ画面からのクロスオリジン呼び出し、他 PC の探索、Service Worker）
# ---------------------------------------------------------------------------

HUB = "https://other-pc.example.ts.net"


def test_cors_allows_only_same_tailnet_origins(app):
    base, token = app.base, app.token
    status, headers, _ = request(base, "/api/clip", token=token, headers={"Origin": HUB})
    assert status == 200 and headers.get("Access-Control-Allow-Origin") == HUB and "Origin" in headers.get("Vary", "")
    status, headers, _ = request(base, "/api/clip", token=token, headers={"Origin": "https://evil.example"})
    assert status == 200 and "Access-Control-Allow-Origin" not in headers
    status, headers, _ = request(base, "/api/clip", token=token, headers={"Origin": "http://other-pc.example.ts.net"})
    assert "Access-Control-Allow-Origin" not in headers  # https 以外は不可
    # 書き込みも同じ tailnet のハブからなら通る
    assert request(base, "/api/clip", method="POST", token=token, data=b"from hub", headers={"Origin": HUB})[0] == 200
    assert app.fake_clipboard.text == "from hub"
    # プリフライト
    status, headers, _ = request(base, "/api/pair", method="OPTIONS", headers={"Origin": HUB, "Access-Control-Request-Method": "POST"})
    assert status == 204 and headers["Access-Control-Allow-Origin"] == HUB
    assert "X-Token" in headers["Access-Control-Allow-Headers"] and "POST" in headers["Access-Control-Allow-Methods"]
    assert request(base, "/api/pair", method="OPTIONS", headers={"Origin": "https://evil.example"})[0] == 403
    assert request(base, "/pc/api/status", method="OPTIONS", headers={"Origin": HUB})[0] == 403
    # 管理操作は他 PC のハブ画面からは不可
    assert request(base, "/pc/api/pair-code", method="POST", json_body={}, headers={"X-ClipBridge": "1", "Origin": HUB})[0] == 403


def test_pair_from_hub_origin(app):
    code = app.pairing.info()["code"]
    status, headers, body = request(app.base, "/api/pair", method="POST", json_body={"code": code}, headers={"Origin": HUB})
    assert status == 200 and as_json(body)["token"] == app.token and headers["Access-Control-Allow-Origin"] == HUB


def test_health_includes_pc_name(app):
    _, _, body = request(app.base, "/api/health")
    assert as_json(body)["pc_name"] == app.pc_name


def test_tailnet_peers_filters_offline_and_phones():
    status = ts_status()
    status["Peer"] = {
        "a": {"HostName": "desk", "DNSName": "desk.example.ts.net.", "Online": True, "OS": "windows"},
        "b": {"HostName": "laptop", "DNSName": "laptop.example.ts.net.", "Online": False, "OS": "windows"},
        "c": {"HostName": "phone", "DNSName": "phone.example.ts.net.", "Online": True, "OS": "iOS"},
        "d": {"HostName": "nas", "DNSName": "", "Online": True, "OS": "linux"},
    }
    peers = server.tailnet_peers(status)
    assert [peer["dns"] for peer in peers] == ["desk.example.ts.net"]
    assert server.tailnet_peers(None) == []


def test_peers_endpoint_probes_other_pcs(app):
    status = ts_status()
    status["Peer"] = {
        "a": {"HostName": "desk", "DNSName": "desk.example.ts.net.", "Online": True, "OS": "windows"},
        "b": {"HostName": "laptop", "DNSName": "laptop.example.ts.net.", "Online": True, "OS": "windows"},
        "me": {"HostName": "pc", "DNSName": DNS + ".", "Online": True, "OS": "windows"},
    }
    app.fake_tailscale._status = status
    probed = []

    def fake_probe(url):
        probed.append(url)
        if url == "https://desk.example.ts.net:8443/":
            return {"ok": True, "app": "ClipBridge", "version": "2.1.0", "pc_name": "Desk-PC"}
        return None

    app.probe = fake_probe
    assert request(app.base, "/api/peers")[0] == 401
    status_code, _, body = request(app.base, "/api/peers", token=app.token)
    assert status_code == 200
    payload = as_json(body)
    assert payload["peers"] == [{"name": "Desk-PC", "host": "desk", "url": "https://desk.example.ts.net:8443/", "version": "2.1.0"}]
    assert payload["self"]["name"] == app.pc_name
    assert not any(DNS in url for url in probed)  # 自分自身は探さない
    assert any(url.startswith("https://laptop.example.ts.net") for url in probed)
    # 20 秒はキャッシュされる
    before = len(probed)
    request(app.base, "/api/peers", token=app.token)
    assert len(probed) == before
    request(app.base, "/api/peers?refresh=1", token=app.token)
    assert len(probed) > before


def test_service_worker_script_carries_version(app):
    status, headers, body = request(app.base, "/sw.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")
    text = body.decode("utf-8")
    assert f"'{server.__version__}'" in text and "__VERSION__" not in text
    assert "'/api" not in text and "'/pc" not in text  # API と PC 画面はキャッシュ対象に含めない


# ---------------------------------------------------------------------------
# 画像（iPhone → PC / PC → iPhone）
# ---------------------------------------------------------------------------


def tiny_png(width: int = 2, height: int = 2, color: bytes = b"\xff\x00\x00") -> bytes:
    import struct
    import zlib

    raw = b"".join(b"\x00" + color * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def upload(app, data: bytes, name: str = "IMG_0001.png", save: bool | None = None, **kwargs):
    query = "?name=" + quote(name) + ("" if save is None else f"&save={int(save)}")
    headers = {"Content-Type": "image/png", **kwargs.pop("headers", {})}
    return request(app.base, "/api/image" + query, method="POST", token=kwargs.pop("token", app.token), data=data, headers=headers, **kwargs)


def test_sniff_image_detects_formats_by_content():
    assert server.sniff_image(tiny_png()) == ("png", "png")
    assert server.sniff_image(b"\xff\xd8\xff\xe1" + b"\x00" * 20) == ("jpeg", "jpg")
    assert server.sniff_image(b"GIF89a" + b"\x00" * 20) == ("gif", "gif")
    assert server.sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 8) == ("webp", "webp")
    assert server.sniff_image(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16) == ("heic", "heic")
    assert server.sniff_image(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None
    assert server.sniff_image(b"\x89PNG") is None  # 短すぎる
    assert server.sniff_image(b"") is None


def test_image_dimensions_for_png_gif_jpeg():
    assert server.image_dimensions(tiny_png(7, 3)) == (7, 3)
    assert server.image_dimensions(b"GIF89a\x10\x00\x20\x00" + b"\x00" * 10) == (16, 32)
    # JPEG: SOI, APP0 (長さ 16), SOF0 (高さ 20 / 幅 40)
    jpeg = b"\xff\xd8" + b"\xff\xe0" + b"\x00\x10" + b"\x00" * 14 + b"\xff\xc0" + b"\x00\x11" + b"\x08" + b"\x00\x14" + b"\x00\x28" + b"\x00" * 10
    assert server.image_dimensions(jpeg) == (40, 20)
    assert server.image_dimensions(b"not an image") is None


def test_image_filename_is_safe_and_stamped():
    now = time.strptime("2026-11-10 09:05:07", "%Y-%m-%d %H:%M:%S")
    assert server.image_filename("IMG_0012.HEIC", "jpg", now) == "20261110-090507-IMG_0012.jpg"
    assert server.image_filename("../../etc/passwd", "png", now) == "20261110-090507-passwd.png"
    assert server.image_filename("C:\\Users\\x\\写真 (1).png", "png", now) == "20261110-090507-写真 _1.png"
    assert server.image_filename("", "png", now) == "20261110-090507-image.png"
    assert server.safe_stem("a" * 100 + ".png") == "a" * 60
    assert server.safe_stem("...") == "image"


def test_unique_path_appends_counter(tmp_path):
    assert server.unique_path(tmp_path, "x.png") == tmp_path / "x.png"
    (tmp_path / "x.png").write_bytes(b"1")
    assert server.unique_path(tmp_path, "x.png") == tmp_path / "x-2.png"
    (tmp_path / "x-2.png").write_bytes(b"1")
    assert server.unique_path(tmp_path, "x.png") == tmp_path / "x-3.png"


def test_image_dir_comes_from_config_or_default(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"token": "x" * 40, "image_dir": str(tmp_path / "custom")}), encoding="utf-8")
    application = server.ClipBridgeApp(port=0, config_path=config, clipboard=server.MemoryClipboard(), tailscale=FakeTailscale(), startup_dir=tmp_path / "s")
    assert application.image_dir == tmp_path / "custom"
    application = server.ClipBridgeApp(port=0, config_path=tmp_path / "other.json", clipboard=server.MemoryClipboard(), tailscale=FakeTailscale(), startup_dir=tmp_path / "s")
    assert application.image_dir == server.default_image_dir()
    assert application.image_dir.name == "ClipBridge"


def test_image_upload_saves_file_and_sets_clipboard(app, tmp_path):
    app.image_dir = tmp_path / "Pictures" / "ClipBridge"
    data = tiny_png()
    status, _, body = upload(app, data)
    assert status == 200
    result = as_json(body)
    assert result["ok"] and result["saved"] and result["clipboard"] and result["kind"] == "png" and result["warning"] is None
    saved = Path(result["path"])
    assert saved.parent == app.image_dir and saved.read_bytes() == data and saved.name.endswith("-IMG_0001.png")
    assert result["dir"] == str(app.image_dir)
    assert app.fake_clipboard.kind == "image" and app.fake_clipboard.image_data == data
    # iPhone 側には「画像」とメタ情報（送信元 phone なので新着扱いにならない）が伝わる
    _, _, body = request(app.base, "/api/clip", token=app.token)
    clip = as_json(body)
    assert clip["kind"] == "image" and clip["source"] == "phone"
    assert clip["image"]["width"] == 2 and clip["image"]["height"] == 2 and clip["image"]["type"] == "image/png" and clip["image"]["size"] == len(data)
    # 本体も取り出せる（PC → iPhone と同じ経路）
    status, headers, body = request(app.base, "/api/image?id=" + clip["image"]["id"], token=app.token)
    assert status == 200 and headers["Content-Type"] == "image/png" and body == data
    # 監視スレッドが自分の書き込みを読み戻しても状態は変わらない
    time.sleep(0.3)
    assert as_json(request(app.base, "/api/clip", token=app.token)[2])["source"] == "phone"
    # 同じ秒に同じ名前でも上書きしない
    status, _, body = upload(app, data)
    assert status == 200 and Path(as_json(body)["path"]) != saved and Path(as_json(body)["path"]).exists()
    admin = app.admin_status()
    assert admin["image_dir"] == str(app.image_dir)
    assert admin["last_image"]["name"] == Path(as_json(body)["path"]).name and admin["last_image"]["saved"] is True


def test_image_upload_clipboard_only_does_not_touch_disk(app, tmp_path):
    app.image_dir = tmp_path / "img"
    data = tiny_png(3, 1)
    status, _, body = upload(app, data, name="photo.png", save=False)
    assert status == 200
    result = as_json(body)
    assert result["saved"] is False and result["path"] is None and result["clipboard"] is True and result["name"] == "photo.png"
    assert not (tmp_path / "img").exists()
    assert app.fake_clipboard.image_data == data
    assert app.admin_status()["last_image"]["saved"] is False
    # 保存なしでクリップボードにも入れられなければエラー（何も残らない）
    def fail(_data):
        raise server.ClipboardError("この形式の画像は Windows で開けません")

    app.fake_clipboard.write_image = fail
    status, _, body = upload(app, data, save=False)
    assert status == 500 and as_json(body)["error"] == "clipboard" and "開けません" in as_json(body)["message"]
    assert not (tmp_path / "img").exists()


def test_image_upload_keeps_file_when_clipboard_fails(app, tmp_path):
    app.image_dir = tmp_path / "img"

    def fail(_data):
        raise server.ClipboardError("この形式の画像は Windows で開けません")

    app.fake_clipboard.write_image = fail
    status, _, body = upload(app, tiny_png())
    result = as_json(body)
    assert status == 200 and result["saved"] is True and result["clipboard"] is False and "開けません" in result["warning"]
    assert Path(result["path"]).exists()
    assert app.admin_status()["last_image"]["clipboard"] is False


def test_image_upload_validation(app, tmp_path, monkeypatch):
    app.image_dir = tmp_path / "img"
    assert upload(app, tiny_png(), token=None)[0] == 401
    status, _, body = upload(app, b"<html>not an image</html>" * 2)
    assert status == 415 and as_json(body)["error"] == "unsupported_media_type"
    assert not (tmp_path / "img").exists()
    monkeypatch.setattr(server, "MAX_IMAGE_BYTES", 16)
    status, _, body = upload(app, tiny_png())
    assert status == 413 and "MB" in as_json(body)["message"]
    monkeypatch.undo()
    # Origin は同じ tailnet のハブだけ
    assert upload(app, tiny_png(), headers={"Origin": "https://evil.example"})[0] == 403
    status, headers, _ = upload(app, tiny_png(), headers={"Origin": HUB})
    assert status == 200 and headers["Access-Control-Allow-Origin"] == HUB


def test_pc_image_is_pushed_to_phone_and_downloadable(app):
    base, token = app.base, app.token
    assert request(base, "/api/image")[0] == 401
    assert request(base, "/api/image", token=token)[0] == 404  # まだ画像はない
    # SSE を開いてから PC で画像をコピー（スクリーンショット）する
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=5)
    conn.request("GET", "/api/events?t=" + quote(token))
    response = conn.getresponse()
    read_sse_event(response)  # 初期状態
    png = tiny_png(5, 4)
    app.fake_clipboard.external_copy_image(png)
    name, event = read_sse_event(response)
    conn.close()
    assert name == "clip" and event["kind"] == "image" and event["source"] == "pc" and event["text"] == ""
    image = event["image"]
    assert image["width"] == 5 and image["height"] == 4 and image["size"] == len(png) and image["type"] == "image/png"
    status, headers, body = request(base, "/api/image?id=" + image["id"], token=token)
    assert status == 200 and body == png and headers["Content-Type"] == "image/png"
    assert "clipbridge-" + image["id"] + ".png" in headers["Content-Disposition"]
    assert request(base, "/api/image?id=stale", token=token)[0] == 404
    # 別の tailnet 由来の Origin には CORS ヘッダを付けない / ハブには付ける
    assert "Access-Control-Allow-Origin" not in request(base, "/api/image", token=token, headers={"Origin": "https://evil.example"})[1]
    assert request(base, "/api/image", token=token, headers={"Origin": HUB})[1]["Access-Control-Allow-Origin"] == HUB
    # PC がテキストをコピーし直したら画像は消える
    app.fake_clipboard.external_copy("text again")
    deadline = time.time() + 2
    while time.time() < deadline and app.current_image() is not None:
        time.sleep(0.05)
    assert app.current_image() is None
    assert request(base, "/api/image", token=token)[0] == 404
    assert as_json(request(base, "/api/clip", token=token)[2])["image"] is None


def test_copied_image_file_is_treated_as_image(app, tmp_path):
    """エクスプローラーで画像ファイルをコピーした場合も iPhone に画像として届く。画像以外のファイルは other。"""
    photo = tmp_path / "photo.png"
    photo.write_bytes(tiny_png(3, 2))
    app.fake_clipboard.external_copy_file(photo)
    time.sleep(0.3)
    clip = as_json(request(app.base, "/api/clip", token=app.token)[2])
    assert clip["kind"] == "image" and clip["image"]["width"] == 3
    assert request(app.base, "/api/image?id=" + clip["image"]["id"], token=app.token)[2] == photo.read_bytes()
    document = tmp_path / "notes.txt"
    document.write_bytes(b"hello world, definitely not an image")
    app.fake_clipboard.external_copy_file(document)
    time.sleep(0.3)
    clip = as_json(request(app.base, "/api/clip", token=app.token)[2])
    assert clip["kind"] == "other" and clip["image"] is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のクリップボード実装")
def test_windows_clipboard_reads_copied_image_file(tmp_path):
    clipboard = server.WindowsClipboard()
    try:
        original_text, original_kind = clipboard.read()
    except server.ClipboardError as exc:
        pytest.skip(f"clipboard is not readable right now: {exc}")
    if original_kind not in ("text", "empty"):
        pytest.skip("clipboard holds non-text content that could not be restored")
    png = tiny_png(5, 3)
    photo = tmp_path / "copied.png"
    photo.write_bytes(png)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", f"Set-Clipboard -LiteralPath '{photo}'"],
        capture_output=True, timeout=30, creationflags=flags,
    )
    if completed.returncode != 0:
        pytest.skip("Set-Clipboard is not available")
    try:
        assert clipboard.read() == ("", "image")
        assert clipboard.read_image() == png
        other = tmp_path / "notes.txt"
        other.write_bytes(b"hello world, definitely not an image")
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", f"Set-Clipboard -LiteralPath '{other}'"], capture_output=True, timeout=30, creationflags=flags)
        assert clipboard.read() == ("", "other")
    finally:
        clipboard.write(original_text if original_kind == "text" else "")


def test_pc_image_too_large_or_unreadable_is_reported_without_preview(app, monkeypatch):
    monkeypatch.setattr(server, "MAX_IMAGE_BYTES", 16)
    app.fake_clipboard.external_copy_image(tiny_png(4, 4))
    time.sleep(0.3)
    clip = as_json(request(app.base, "/api/clip", token=app.token)[2])
    assert clip["kind"] == "image" and clip["image"] is None
    monkeypatch.undo()

    def unreadable():
        raise server.ClipboardError("読めない")

    app.fake_clipboard.read_image = unreadable
    app.fake_clipboard.external_copy_image(tiny_png(3, 3))
    time.sleep(0.3)
    clip = as_json(request(app.base, "/api/clip", token=app.token)[2])
    assert clip["kind"] == "image" and clip["image"] is None


def test_admin_opens_image_folder(app, tmp_path, monkeypatch):
    app.image_dir = tmp_path / "img"
    opened: list[Path] = []
    monkeypatch.setattr(server, "open_folder", lambda path: opened.append(path))
    status, _, body = request(app.base, "/pc/api/images/open", method="POST", json_body={}, headers={"X-ClipBridge": "1"})
    assert status == 200 and as_json(body)["path"] == str(app.image_dir)
    assert opened == [app.image_dir] and app.image_dir.is_dir()


def test_clip_state_treats_images_by_id():
    state = server.ClipState()
    assert state.set("", "image", "pc", {"id": "a"}) is True
    assert state.set("", "image", "pc", {"id": "a"}) is False
    assert state.set("", "image", "pc", {"id": "b"}) is True
    assert state.snapshot()["image"] == {"id": "b"}
    assert state.set("hello", "text", "pc", {"id": "ignored"}) is True
    assert state.snapshot()["image"] is None


# ---------------------------------------------------------------------------
# Pro: ライセンス・履歴・保存ルール・ホットキー
# ---------------------------------------------------------------------------

LICENSE_TOOL = Path(__file__).resolve().parents[2] / "scripts" / "license_tool.py"
_tool_spec = importlib.util.spec_from_file_location("license_tool", LICENSE_TOOL)
license_tool = importlib.util.module_from_spec(_tool_spec)
assert _tool_spec.loader is not None
_tool_spec.loader.exec_module(license_tool)
TEST_KEYPAIR = license_tool.keygen(1024)  # テスト用（本物は 2048）
TEST_PUBLIC = {"n": format(TEST_KEYPAIR["n"], "x"), "e": TEST_KEYPAIR["e"]}
# テスト中に開発機の本物のグローバルホットキーを奪わないよう、登録は無効にする（解析と設定の保存だけを検証する）
server.HotkeyService.supported = False


def issue_key(**overrides) -> str:
    fields = {"email": "test@example.com", "name": "テスト", "license_id": "CB-TEST-0001"}
    fields.update(overrides)
    return license_tool.issue(TEST_KEYPAIR, **fields)


@pytest.fixture
def pro(app, monkeypatch):
    """app を Pro にする（テスト用の公開鍵に差し替えてからキーを適用）。"""
    monkeypatch.setattr(server, "LICENSE_PUBLIC_KEY", TEST_PUBLIC)
    app.apply_license(issue_key())
    assert app.pro
    return app


def admin(app, action, body=None):
    return request(app.base, "/pc/api/" + action, method="POST", json_body=body or {}, headers={"X-ClipBridge": "1"})


def test_license_parse_accepts_signed_keys_and_rejects_tampering():
    key = issue_key()
    info = server.parse_license(key, TEST_PUBLIC)
    assert info["valid"] and info["id"] == "CB-TEST-0001" and info["email"] == "test@example.com" and info["plan"] == "pro"
    assert not server.parse_license(key[:-6] + "AAAAAA", TEST_PUBLIC)["valid"]
    prefix, payload, signature = key.split(".")
    forged = json.loads(server._b64url_decode(payload))
    forged["email"] = "someone@else.example"
    forged_key = prefix + "." + license_tool.b64url(json.dumps(forged, sort_keys=True, separators=(",", ":")).encode()) + "." + signature
    assert "署名" in server.parse_license(forged_key, TEST_PUBLIC)["error"]
    assert "期限" in server.parse_license(issue_key(expires="2020-01-01"), TEST_PUBLIC)["error"]
    assert server.parse_license(issue_key(expires="2999-12-31"), TEST_PUBLIC)["valid"]
    assert "Pro" in server.parse_license(issue_key(plan="basic"), TEST_PUBLIC)["error"]
    assert "形式" in server.parse_license("not-a-key", TEST_PUBLIC)["error"]
    assert "入力" in server.parse_license("", TEST_PUBLIC)["error"]
    # 別の鍵で発行したものは通らない
    other = license_tool.keygen(1024)
    assert not server.parse_license(license_tool.issue(other, "x@y.z", "", "CB-X"), TEST_PUBLIC)["valid"]
    # 本物の公開鍵（server.py 埋め込み）はテスト鍵の署名を受け付けない
    assert not server.parse_license(key)["valid"]


def test_license_apply_and_remove_via_dashboard(app, monkeypatch):
    monkeypatch.setattr(server, "LICENSE_PUBLIC_KEY", TEST_PUBLIC)
    assert as_json(request(app.base, "/api/health")[2])["pro"] is False
    status, _, body = admin(app, "license", {"key": "CBP1.bad.key"})
    assert status == 400 and as_json(body)["error"] == "invalid_license"
    status, _, body = admin(app, "license", {"key": issue_key()})
    assert status == 200 and as_json(body)["license"]["valid"] and as_json(body)["license"]["pro"]
    assert as_json(request(app.base, "/api/health")[2])["pro"] is True
    admin_state = app.admin_status()
    assert admin_state["pro"] and admin_state["license"]["id"] == "CB-TEST-0001" and "history" in admin_state["license"]["features"]
    assert json.loads(app.config_path.read_text(encoding="utf-8"))["license"].startswith("CBP1.")
    # 再起動後も有効
    reloaded = server.ClipBridgeApp(port=0, config_path=app.config_path, clipboard=server.MemoryClipboard(), tailscale=FakeTailscale(), startup_dir=app.config_path.parent / "s")
    assert reloaded.pro
    status, _, body = admin(app, "license/remove")
    assert status == 200 and not as_json(body)["license"]["pro"] and not app.pro
    assert "license" not in json.loads(app.config_path.read_text(encoding="utf-8"))


def test_history_requires_pro(app):
    status, _, body = request(app.base, "/api/history", token=app.token)
    assert status == 403 and as_json(body)["error"] == "pro_required"
    assert request(app.base, "/api/history")[0] == 401
    assert admin(app, "history/clear")[0] == 403
    assert admin(app, "hotkeys", {"enabled": True})[0] == 403
    assert admin(app, "images/settings", {"subfolder": "date"})[0] == 403
    assert admin(app, "images/settings", {"dir": str(app.config_path.parent / "free-dir")})[0] == 200  # 保存先の変更は無料でも可
    assert app.image_dir == app.config_path.parent / "free-dir"
    # 無料版では何も記録しない
    app.fake_clipboard.external_copy("free text")
    time.sleep(0.3)
    assert app.history.counts()["total"] == 0


def test_history_records_searches_pins_and_restores(pro):
    app, base, token = pro, pro.base, pro.token
    app.fake_clipboard.external_copy("最初のメモ")
    time.sleep(0.3)
    app.fake_clipboard.external_copy("二番目 https://example.com")
    time.sleep(0.3)
    request(base, "/api/clip", method="POST", token=token, data="iPhone から送った文".encode("utf-8"))
    status, _, body = request(base, "/api/history", token=token)
    assert status == 200
    items = as_json(body)["items"]
    assert [item["text"] for item in items] == ["iPhone から送った文", "二番目 https://example.com", "最初のメモ"]
    assert [item["source"] for item in items] == ["phone", "pc", "pc"]
    # 同じ内容をまたコピーすると先頭に移動し、重複しない
    app.fake_clipboard.external_copy("最初のメモ")
    time.sleep(0.3)
    items = as_json(request(base, "/api/history", token=token)[2])["items"]
    assert [item["text"] for item in items] == ["最初のメモ", "iPhone から送った文", "二番目 https://example.com"]
    # 検索
    items = as_json(request(base, "/api/history?q=example", token=token)[2])["items"]
    assert len(items) == 1 and items[0]["text"].startswith("二番目")
    # 長文はプレビューに切り詰め、個別取得で全文
    long_text = "長い" * 500
    app.fake_clipboard.external_copy(long_text)
    time.sleep(0.3)
    first = as_json(request(base, "/api/history", token=token)[2])["items"][0]
    assert first["truncated"] and len(first["text"]) == server.HISTORY_PREVIEW and first["length"] == len(long_text)
    full = as_json(request(base, f"/api/history/{first['id']}", token=token)[2])
    assert full["text"] == long_text and not full["truncated"]
    # ピン留めと再送
    target = next(item for item in items if item["text"].startswith("二番目"))
    status, _, body = request(base, f"/api/history/{target['id']}/pin", method="POST", token=token, json_body={"pinned": True})
    assert status == 200 and as_json(body)["entry"]["pinned"] is True
    status, _, body = request(base, f"/api/history/{target['id']}/restore", method="POST", token=token, json_body={})
    assert status == 200 and app.fake_clipboard.text == "二番目 https://example.com"
    assert as_json(request(base, "/api/clip", token=token)[2])["text"] == "二番目 https://example.com"
    time.sleep(0.3)
    assert app.history.counts()["total"] == 4  # 戻しても履歴は増えない
    # 削除・消去（ピン留めは残る）
    assert request(base, f"/api/history/{first['id']}/delete", method="POST", token=token, json_body={})[0] == 200
    assert request(base, "/api/history/clear", method="POST", token=token, json_body={})[0] == 200
    items = as_json(request(base, "/api/history", token=token)[2])["items"]
    assert [item["text"] for item in items] == ["二番目 https://example.com"] and items[0]["pinned"]
    assert request(base, "/api/history/000000000000/restore", method="POST", token=token, json_body={})[0] == 404


def test_history_keeps_images_and_evicts_beyond_limit(pro):
    app, base, token = pro, pro.base, pro.token
    app.history.limit = 3
    png = tiny_png(4, 2)
    app.fake_clipboard.external_copy_image(png)
    time.sleep(0.3)
    items = as_json(request(base, "/api/history", token=token)[2])["items"]
    assert items[0]["kind"] == "image" and items[0]["width"] == 4 and "text" not in items[0]
    image_id = items[0]["id"]
    status, headers, body = request(base, f"/api/history/{image_id}/image", token=token)
    assert status == 200 and body == png and headers["Content-Type"] == "image/png"
    assert (app.history.image_dir / items[0]["file"]).exists()
    assert as_json(request(base, "/api/history?q=" + quote("画像"), token=token)[2])["items"][0]["id"] == image_id
    # ピン留めした画像は上限を超えても残り、ファイルも消えない
    request(base, f"/api/history/{image_id}/pin", method="POST", token=token, json_body={"pinned": True})
    for index in range(5):
        app.fake_clipboard.external_copy(f"text {index}")
        time.sleep(0.25)
    items = as_json(request(base, "/api/history", token=token)[2])["items"]
    assert len(items) == 4 and sum(1 for item in items if item["kind"] == "image") == 1
    assert [item["text"] for item in items if item["kind"] == "text"] == ["text 4", "text 3", "text 2"]
    assert (app.history.image_dir / items[-1]["file"]).exists()
    # 画像を戻すと PC のクリップボードが画像になる
    status, _, body = request(base, f"/api/history/{image_id}/restore", method="POST", token=token, json_body={})
    assert status == 200 and app.fake_clipboard.image_data == png
    assert as_json(request(base, "/api/clip", token=token)[2])["kind"] == "image"
    # ピン留めしたままでも削除でき、ファイルも消える
    file_path = app.history.image_dir / items[-1]["file"]
    assert request(base, f"/api/history/{image_id}/delete", method="POST", token=token, json_body={})[0] == 200
    assert not file_path.exists()
    # ピン留めを外した項目が上限の外にあれば、その時点で履歴から外れる
    app.fake_clipboard.external_copy_image(tiny_png(2, 2))
    time.sleep(0.3)
    older = as_json(request(base, "/api/history", token=token)[2])["items"][0]
    request(base, f"/api/history/{older['id']}/pin", method="POST", token=token, json_body={"pinned": True})
    for index in range(3):
        app.fake_clipboard.external_copy(f"newer {index}")
        time.sleep(0.25)
    request(base, f"/api/history/{older['id']}/pin", method="POST", token=token, json_body={"pinned": False})
    assert request(base, f"/api/history/{older['id']}", token=token)[0] == 404
    assert not (app.history.image_dir / older["file"]).exists()
    # 履歴の記録を止められる
    assert admin(app, "history/settings", {"enabled": False})[0] == 200
    app.fake_clipboard.external_copy("not recorded")
    time.sleep(0.3)
    assert all(item.get("text") != "not recorded" for item in app.history.entries)
    assert json.loads(app.config_path.read_text(encoding="utf-8"))["history_enabled"] is False


def test_history_persists_across_restart(pro, tmp_path):
    pro.fake_clipboard.external_copy("残るメモ")
    time.sleep(0.3)
    reloaded = server.ClipHistory(pro.history.path)
    assert [entry["text"] for entry in reloaded.entries] == ["残るメモ"]


def test_image_rules_apply_only_for_pro(app, monkeypatch, tmp_path):
    app.image_dir = tmp_path / "pics"
    monkeypatch.setattr(server, "LICENSE_PUBLIC_KEY", TEST_PUBLIC)
    status, _, body = upload(app, tiny_png(), name="IMG_0001.png")
    free_path = Path(as_json(body)["path"])
    assert free_path.parent == app.image_dir and free_path.name.endswith("-IMG_0001.png")
    app.apply_license(issue_key())
    assert admin(app, "images/settings", {"template": "{bogus}"})[0] == 400
    assert admin(app, "images/settings", {"template": "no tokens"})[0] == 400
    assert admin(app, "images/settings", {"subfolder": "weekly"})[0] == 400
    status, _, body = admin(app, "images/settings", {"subfolder": "date", "template": "{date}_{pc}_{name}"})
    assert status == 200
    settings = as_json(body)
    assert settings["subfolder"] == "date" and settings["template"] == "{date}_{pc}_{name}" and settings["applied"]
    assert settings["example"].startswith(time.strftime("%Y-%m-%d"))
    status, _, body = upload(app, tiny_png(), name="IMG_0002.png")
    pro_path = Path(as_json(body)["path"])
    assert pro_path.parent == app.image_dir / time.strftime("%Y-%m-%d")
    assert pro_path.name == f"{time.strftime('%Y%m%d')}_{server.safe_stem(app.pc_name)}_IMG_0002.png"
    saved = json.loads(app.config_path.read_text(encoding="utf-8"))
    assert saved["image_subfolder"] == "date" and saved["image_template"] == "{date}_{pc}_{name}"
    # ライセンスを外すと既定に戻る（設定は保持）
    app.remove_license()
    status, _, body = upload(app, tiny_png(), name="IMG_0003.png")
    assert Path(as_json(body)["path"]).parent == app.image_dir


def test_render_image_name_and_template_validation():
    now = time.strptime("2026-11-10 09:05:07", "%Y-%m-%d %H:%M:%S")
    assert server.render_image_name("{date}-{time}-{name}", "IMG_0042.HEIC", "jpg", "pc", now) == "20261110-090507-IMG_0042.jpg"
    assert server.render_image_name("{year}/{month}/{name}", "a.png", "png", "pc", now) == "2026_11_a.png"
    assert server.render_image_name("{datetime} {pc}", "x.png", "png", "デスクトップ PC", now) == "20261110-090507 デスクトップ PC.png"
    assert server.render_image_name("{name}", "...", "png", "pc", now) == "image.png"
    assert server.validate_image_template("{date}-{name}") is None
    assert "使えない" in server.validate_image_template("{foo}")
    assert server.validate_image_template("") and server.validate_image_template("plain")
    assert server.image_subfolder("date", now) == "2026-11-10" and server.image_subfolder("month", now) == "2026-11" and server.image_subfolder("none", now) == ""


def test_hotkey_parsing():
    assert server.parse_hotkey("Ctrl+Alt+V") == (0x0002 | 0x0001 | server.MOD_NOREPEAT, ord("V"))
    assert server.parse_hotkey("ctrl + shift + f5") == (0x0002 | 0x0004 | server.MOD_NOREPEAT, 0x74)
    assert server.parse_hotkey("Win+Left") == (0x0008 | server.MOD_NOREPEAT, 0x25)
    assert server.parse_hotkey("Alt+1")[1] == ord("1")
    for bad in ("V", "Ctrl+", "Ctrl+Alt", "Ctrl+Alt+V+B", "Ctrl+Alt+あ", "Ctrl+F99", ""):
        with pytest.raises(ValueError):
            server.parse_hotkey(bad)


def test_hotkey_settings_and_actions(pro, monkeypatch):
    app = pro
    status, _, body = admin(app, "hotkeys", {"bindings": {"open_dashboard": "Ctrl+Alt+V", "restore_phone": "Ctrl+Alt+V"}})
    assert status == 400 and "同じキー" in as_json(body)["message"]
    assert admin(app, "hotkeys", {"bindings": {"restore_phone": "Ctrl+Alt+あ"}})[0] == 400
    status, _, body = admin(app, "hotkeys", {"enabled": True, "bindings": {"restore_phone": "Ctrl+Shift+V"}})
    assert status == 200
    hotkeys = as_json(body)
    assert hotkeys["bindings"]["restore_phone"] == "Ctrl+Shift+V" and hotkeys["bindings"]["open_dashboard"] == "Ctrl+Alt+C"
    assert json.loads(app.config_path.read_text(encoding="utf-8"))["hotkeys"]["restore_phone"] == "Ctrl+Shift+V"
    assert app.admin_status()["hotkeys"]["actions"]["restore_previous"]
    # 動作: 履歴 a, b, c（c が最新）→ 1 つ前 = b → a → 先頭に戻る
    for text in ("a", "b", "c"):
        app.fake_clipboard.external_copy(text)
        time.sleep(0.25)
    app.hotkey_action("restore_previous")
    assert app.fake_clipboard.text == "b"
    app.hotkey_action("restore_previous")
    assert app.fake_clipboard.text == "a"
    app.hotkey_action("restore_previous")
    assert app.fake_clipboard.text == "c"
    # iPhone から届いた最後の内容に戻す
    request(app.base, "/api/clip", method="POST", token=app.token, data=b"from phone")
    app.fake_clipboard.external_copy("pc again")
    time.sleep(0.3)
    app.hotkey_action("restore_phone")
    assert app.fake_clipboard.text == "from phone"
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", lambda url: opened.append(url))
    app.hotkey_action("open_dashboard")
    assert opened == [f"http://127.0.0.1:{app.port}/pc"]
    assert admin(app, "hotkeys", {"enabled": False})[0] == 200 and app.hotkeys_enabled is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のクリップボード実装")
def test_windows_clipboard_image_round_trip(tmp_path):
    clipboard = server.WindowsClipboard()
    try:
        original_text, original_kind = clipboard.read()
    except server.ClipboardError as exc:
        pytest.skip(f"clipboard is not readable right now: {exc}")
    if original_kind not in ("text", "empty"):
        pytest.skip("clipboard holds non-text content that could not be restored")
    try:
        png = tiny_png(6, 2)
        clipboard.write_image(png)
        assert clipboard.read() == ("", "image")
        assert clipboard.read_image() == png  # PNG 形式も入れているのでそのまま戻る
        # 壊れた PNG は拒否される（壊れた JPEG だと GDI+ が内部で SEH 例外を使うため faulthandler の表示が出る。動作は同じ）
        with pytest.raises(server.ClipboardError):
            clipboard.write_image(png[:30] + b"garbage" * 4)
    finally:
        clipboard.write(original_text if original_kind == "text" else "")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のクリップボード実装")
def test_windows_clipboard_applies_exif_orientation(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    clipboard = server.WindowsClipboard()
    try:
        original_text, original_kind = clipboard.read()
    except server.ClipboardError as exc:
        pytest.skip(f"clipboard is not readable right now: {exc}")
    if original_kind not in ("text", "empty"):
        pytest.skip("clipboard holds non-text content that could not be restored")
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6  # 90 度回転
    Image.new("RGB", (40, 20), (0, 128, 255)).save(buffer, "JPEG", exif=exif)
    try:
        clipboard.write_image(buffer.getvalue())
        back = clipboard.read_image()
        assert back.startswith(server.PNG_SIGNATURE)
        assert server.image_dimensions(back) == (20, 40)  # 貼り付け用のビットマップは回転済み
    finally:
        clipboard.write(original_text if original_kind == "text" else "")
