"""ClipBridge: loopback-only text clipboard bridge."""

from __future__ import annotations

import hashlib
import base64
import io
import hmac
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def windows_clipboard_fallback():
    """Use Win32 Unicode text when package installation is unavailable."""
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    @contextmanager
    def opened():
        for _ in range(10):
            if user32.OpenClipboard(None):
                break
            time.sleep(0.05)
        else:
            raise OSError("Windows clipboard is busy")
        try:
            yield
        finally:
            user32.CloseClipboard()

    def paste():
        with opened():
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                raise OSError("Cannot read Windows clipboard")
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise OSError("Cannot lock Windows clipboard")
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)

    def copy(value):
        encoded = value.encode("utf-16-le") + b"\0\0"
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not handle:
            raise MemoryError("Cannot allocate clipboard memory")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            raise OSError("Cannot lock clipboard memory")
        ctypes.memmove(pointer, encoded, len(encoded))
        kernel32.GlobalUnlock(handle)
        with opened():
            if not user32.EmptyClipboard() or not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                raise OSError("Cannot write Windows clipboard")

    return SimpleNamespace(copy=copy, paste=paste)


try:
    import pyperclip
    USING_FALLBACK = False
except ModuleNotFoundError:
    if sys.platform != "win32":
        raise
    pyperclip = windows_clipboard_fallback()
    USING_FALLBACK = True


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ClipBridge" if getattr(sys, "frozen", False) else ROOT
DATA.mkdir(parents=True, exist_ok=True)
MAX_BYTES = 1024 * 1024
HOST = "127.0.0.1"
PORT = 8787


def console_message(message: str) -> None:
    """pythonw.exe has no console streams; requests must still work there."""
    if sys.stdout is not None:
        print(message, flush=True)


def load_token(path: Path = DATA / "config.json") -> str:
    if not path.exists():
        token = secrets.token_urlsafe(32)
        try:
            with path.open("x", encoding="utf-8") as file:
                json.dump({"token": token}, file)
        except FileExistsError:
            pass
    data = json.loads(path.read_text(encoding="utf-8"))
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("config.json の token が不正です")
    return token


def tailscale_path() -> str | None:
    installed = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tailscale/tailscale.exe"
    return str(installed) if installed.is_file() else shutil.which("tailscale")


def magic_dns_name() -> str | None:
    try:
        result = subprocess.run(
            [tailscale_path() or "tailscale", "status", "--json"], capture_output=True, text=True,
            timeout=5, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return json.loads(result.stdout)["Self"]["DNSName"].rstrip(".") or None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def connection_info() -> dict:
    """Diagnose this PC only; never infer the iPhone's VPN state."""
    info = {"pc_name": socket.gethostname(), "tailscale": "missing", "dns_name": None,
            "https_ready": None}
    if not tailscale_path():
        return info
    def run_json(*args):
        result = subprocess.run(
            [tailscale_path(), *args], capture_output=True, text=True, timeout=5, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return json.loads(result.stdout)
    try:
        status = run_json("status", "--json")
        info["tailscale"] = "connected" if status.get("BackendState") == "Running" else "disconnected"
        info["dns_name"] = status.get("Self", {}).get("DNSName", "").rstrip(".") or None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        info["tailscale"] = "unknown"
        return info
    try:
        serve = run_json("serve", "status", "--json")
        info["https_ready"] = bool(
            serve.get("TCP", {}).get("443", {}).get("HTTPS") and any(
                value.get("Handlers", {}).get("/", {}).get("Proxy") in
                ("http://127.0.0.1:8787", "http://localhost:8787")
                for key, value in serve.get("Web", {}).items() if key.endswith(":443")
            )
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        pass
    return info


def create_server(port: int = PORT, token: str | None = None) -> ThreadingHTTPServer:
    """Create a server. Port 0 is supported for integration tests."""
    expected_token = token if token is not None else load_token()
    state_lock = threading.Lock()
    state = {"hash": None, "updated": 0.0}

    class Handler(BaseHTTPRequestHandler):
        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            # BaseHTTPRequestHandler normally logs the complete request target.
            self.log_message("HTTP %s", code)

        def log_message(self, format: str, *args: object) -> None:
            # Never include a request body or clipboard text in logs.
            console_message("[%s] %s" % (self.log_date_time_string(), format % args))

        def respond(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def static(self, path: Path, content_type: str) -> None:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self) -> bool:
            supplied = self.headers.get("X-Token", "")
            if not hmac.compare_digest(supplied, expected_token):
                self.respond(401, {"error": "セットアップURLからもう一度開いてください"})
                return False
            return True

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/":
                return self.static(WEB / "index.html", "text/html; charset=utf-8")
            if path == "/manifest.webmanifest":
                return self.static(WEB / "manifest.webmanifest", "application/manifest+json")
            if path == "/icon-180.png":
                return self.static(WEB / "icon-180.png", "image/png")
            if path not in ("/api/health", "/api/clip", "/api/setup", "/api/connection"):
                return self.respond(404, {"error": "見つかりません"})
            if not self.authorized():
                return
            if path == "/api/health":
                return self.respond(200, {"ok": True})
            if path == "/api/connection":
                return self.respond(200, connection_info())
            if path == "/api/setup":
                name = magic_dns_name()
                if not name:
                    return self.respond(503, {"error": "Tailscale の接続状態を確認してください。"})
                import qrcode
                import qrcode.image.svg
                url = f"https://{name}/?setup=1#t={expected_token}"
                output = io.BytesIO()
                qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage).save(output)
                return self.respond(200, {"url": url, "qr": "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode("ascii")})
            try:
                value = pyperclip.paste()
                if not isinstance(value, str):
                    raise TypeError("クリップボードの値がテキストではありません")
                digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
                with state_lock:
                    if digest != state["hash"]:
                        state["hash"] = digest
                        state["updated"] = time.time()
                    updated = state["updated"]
                self.log_message("clipboard read: %d chars", len(value))
                return self.respond(200, {"text": value, "hash": digest, "updated": updated})
            except Exception as exc:
                self.log_message("clipboard read failed: %s", type(exc).__name__)
                return self.respond(500, {"error": "PCのクリップボードを読み取れませんでした"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path != "/api/clip":
                return self.respond(405 if path in ("/", "/api/health", "/manifest.webmanifest", "/icon-180.png") else 404,
                                    {"error": "使用できないメソッドです" if path in ("/", "/api/health", "/manifest.webmanifest", "/icon-180.png") else "見つかりません"})
            if not self.authorized():
                return
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme not in ("https", "http") or parsed.netloc.lower() != self.headers.get("Host", "").lower():
                    return self.respond(403, {"error": "Origin が一致しません"})
            if self.headers.get("Content-Type", "").lower() != "text/plain; charset=utf-8":
                return self.respond(415, {"error": "text/plain; charset=utf-8 を指定してください"})
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self.respond(411, {"error": "Content-Length が必要です"})
            if length < 0:
                return self.respond(400, {"error": "Content-Length が不正です"})
            if length > MAX_BYTES:
                # Drain a bounded amount so a small oversized upload can receive
                # the JSON response before Windows closes its sending socket.
                self.rfile.read(MAX_BYTES + 1)
                self.close_connection = True
                return self.respond(413, {"error": "1MBを超えています"})
            body = self.rfile.read(length)
            try:
                value = body.decode("utf-8")
            except UnicodeDecodeError:
                return self.respond(400, {"error": "UTF-8 テキストが必要です"})
            try:
                pyperclip.copy(value)
                digest = hashlib.sha256(body).hexdigest()
                with state_lock:
                    state["hash"] = digest
                    state["updated"] = time.time()
                self.log_message("clipboard written: %d chars", len(value))
                return self.respond(200, {"ok": True, "length": len(value)})
            except Exception as exc:
                self.log_message("clipboard write failed: %s", type(exc).__name__)
                return self.respond(500, {"error": "PCのクリップボードへ書き込めませんでした"})

        def do_PUT(self) -> None:
            self.respond(405 if urlsplit(self.path).path in ("/", "/api/health", "/api/clip", "/manifest.webmanifest", "/icon-180.png") else 404,
                         {"error": "使用できないメソッドです"})

        do_DELETE = do_PUT
        do_PATCH = do_PUT

    class ExclusiveHTTPServer(ThreadingHTTPServer):
        def server_bind(self):
            if sys.platform == "win32":
                # SO_REUSEADDR permits duplicate listeners on Windows. Requests
                # could then reach an older instance with a different token.
                self.allow_reuse_address = False
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    return ExclusiveHTTPServer((HOST, port), Handler)


def main() -> None:
    token = load_token()
    name = magic_dns_name() or "<MagicDNS名>"
    console_message(f"ClipBridge: http://{HOST}:{PORT}")
    console_message(f"セットアップURL: https://{name}/#t={token}")
    console_message("このURLはトークンを含みます。共有しないでください。")
    server = create_server(token=token)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
