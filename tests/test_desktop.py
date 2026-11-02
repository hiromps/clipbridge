import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import desktop
import server


def manifest(data=b"MZfree-app", version="1.2.0"):
    return {"version": version,
            "url": f"https://github.com/hiromps/clipbridge/releases/download/v{version}/ClipBridge.exe",
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


@pytest.mark.parametrize("change", [
    {"url": "http://github.com/hiromps/clipbridge/releases/download/v1.2.0/ClipBridge.exe"},
    {"url": "https://github.com/other/repo/releases/download/v1.2.0/ClipBridge.exe"},
    {"version": "1.2.0;command"}, {"sha256": "missing"}, {"size": 0}, {"size": True},
    {"size": 200 * 1024 * 1024},
])
def test_reject_untrusted_update_manifest(change):
    with pytest.raises((ValueError, TypeError)):
        desktop.validate_manifest(manifest() | change)


def test_version_order():
    assert desktop.version_tuple("1.10.0") > desktop.version_tuple("1.2.0")
    assert desktop.validate_manifest(manifest())["version"] == "1.2.0"


class Download(io.BytesIO):
    url = "https://release-assets.githubusercontent.com/example"


def test_update_download_verifies_before_apply(monkeypatch, tmp_path):
    data = b"MZfree-app"
    monkeypatch.setattr(desktop.tempfile, "mkdtemp", lambda **_: str(tmp_path))
    monkeypatch.setattr(desktop.urllib.request, "urlopen", lambda *a, **kw: Download(data))
    result = desktop.download_update(manifest(data))
    assert result.read_bytes() == data


@pytest.mark.parametrize("received", [b"MZbad-data", b"MZshort", b"MZtoo-long-application"])
def test_corrupt_update_is_removed(monkeypatch, tmp_path, received):
    directory = tmp_path / "update"
    directory.mkdir()
    monkeypatch.setattr(desktop.tempfile, "mkdtemp", lambda **_: str(directory))
    monkeypatch.setattr(desktop.urllib.request, "urlopen", lambda *a, **kw: Download(received))
    with pytest.raises(ValueError):
        desktop.download_update(manifest())
    assert not directory.exists()


def test_refuse_other_services():
    assert desktop.serve_conflict({"Web": {"pc:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}})
    assert desktop.serve_conflict({"Web": {"pc:443": {"Handlers": {"/other": {"Path": "C:/data"}}}}})
    assert desktop.serve_conflict({"TCP": {"443": {"TCPForward": "localhost:80"}}})
    assert not desktop.serve_conflict({})
    assert not desktop.serve_conflict({"Web": {"pc:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}}})


def test_tailscale_detection_without_path(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda _: None)
    monkeypatch.setattr(server.os, "environ", {"ProgramFiles": "C:/does-not-exist"})
    assert server.tailscale_path() is None


def test_state_persists_without_token(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "HOME", tmp_path)
    monkeypatch.setattr(desktop, "STATE_FILE", tmp_path / "desktop.json")
    desktop.save_state({"step": 2, "current_executable": "sample.exe"})
    assert desktop.read_state()["step"] == 2
    assert not (tmp_path / "desktop.tmp").exists()


def test_bundled_web_auth_and_clipboard(monkeypatch):
    clip = {"value": "original"}
    monkeypatch.setattr(server.pyperclip, "paste", lambda: clip["value"])
    monkeypatch.setattr(server.pyperclip, "copy", lambda value: clip.update(value=value))
    app = server.create_server(port=0, token="test-token")
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{app.server_address[1]}"
    try:
        assert desktop.health("test-token", base)
        assert not desktop.health("wrong", base)
        request = urllib.request.Request(base + "/api/clip", data="日本語🙂".encode(), headers={"X-Token": "test-token", "Content-Type": "text/plain; charset=utf-8"})
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["ok"]
        assert clip["value"] == "日本語🙂"
        for path in ("/", "/icon-180.png", "/manifest.webmanifest"):
            with urllib.request.urlopen(base + path) as response:
                assert response.status == 200
    finally:
        app.shutdown()
        app.server_close()


def test_changing_page_preserves_install_pointer(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "HOME", tmp_path)
    monkeypatch.setattr(desktop, "STATE_FILE", tmp_path / "desktop.json")
    desktop.save_state({"current_executable": "installed.exe", "previous_executable": "old.exe"})
    # The native window's page state must never overwrite newer installer state.
    app = desktop.DesktopApp.__new__(desktop.DesktopApp)
    app.state = {"step": 0}
    app.content = SimpleNamespace(winfo_children=lambda: [])
    app.text = lambda *a, **kw: None
    app.button = lambda *a, **kw: None
    app.show(2)
    assert desktop.read_state()["current_executable"] == "installed.exe"
    assert desktop.read_state()["previous_executable"] == "old.exe"


def test_user_install_keeps_tokens_and_previous_version(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "HOME", tmp_path)
    monkeypatch.setattr(desktop, "STATE_FILE", tmp_path / "desktop.json")
    executable = tmp_path / "download.exe"
    executable.write_bytes(b"MZsample")
    previous = tmp_path / "versions/1.0.0/ClipBridge.exe"
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b"MZprevious")
    (tmp_path / "config.json").write_text('{"token":"private-keep-me"}')
    desktop.save_state({"current_executable": str(previous)})
    monkeypatch.setattr(desktop.sys, "frozen", True, raising=False)
    monkeypatch.setattr(desktop.sys, "executable", str(executable))
    calls = []
    monkeypatch.setattr(desktop.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    installed = desktop.install_for_user()
    assert installed.read_bytes() == b"MZsample"
    assert previous.exists()
    assert desktop.read_state()["previous_executable"] == str(previous)
    assert json.loads((tmp_path / "config.json").read_text())["token"] == "private-keep-me"
    assert calls[0][0][0] == "powershell.exe"
    assert calls[0][1]["creationflags"] == desktop.NO_WINDOW


def test_repair_never_changes_another_services_config(monkeypatch):
    monkeypatch.setattr(server, "tailscale_path", lambda: "C:/Tailscale/tailscale.exe")
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps({"Web": {"pc:443": {"Handlers": {"/": {"Proxy": "http://localhost:3000"}}}}})))
    with pytest.raises(RuntimeError, match="443"):
        desktop.configure_https()
