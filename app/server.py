#!/usr/bin/env python3
"""ClipBridge — iPhone と Windows PC のクリップボードを Tailscale 経由で共有するローカルサーバー。

外部ライブラリ不要（Python 3.10 以上の標準ライブラリのみ）。

    python server.py               # 起動して PC 用のセットアップ画面をブラウザーで開く
    python server.py --no-browser  # 画面を開かずに起動（自動起動用）
    python server.py --startup on  # Windows サインイン時の自動起動を登録（off で解除）

配布版（ClipBridge.exe、PyInstaller）も同じファイルから作る。exe 版は設定を
%LOCALAPPDATA%\\ClipBridge に置き（旧版と共通）、GitHub Releases の更新を PC 画面から適用できる。

設計:
  - サーバーは 127.0.0.1 のみで待ち受け、iPhone からは Tailscale Serve の HTTPS 経由で届く
  - iPhone 用 API はトークン認証（X-Token ヘッダ、または SSE 用に ?t= クエリ）
  - PC 用の管理画面 /pc は「ループバックから直接」来たリクエストにだけ応答する
  - ペアリングは 6 桁コード（QR にも埋め込む）。成功または 5 回失敗でコードを更新する
  - PC のクリップボード変化は SSE (/api/events) で iPhone にプッシュする
  - クリップボードの中身・トークン・コードはログに出さない
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

__version__ = "2.0.0"
APP_NAME = "ClipBridge"
FROZEN = bool(getattr(sys, "frozen", False))  # PyInstaller でビルドした exe 版か
ROOT = Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", ROOT))  # exe 版は同梱ファイルの展開先
WEB_DIR = BUNDLE_DIR / "web"
# exe 版の設定は旧版（v1）と同じ %LOCALAPPDATA%\ClipBridge に置き、トークンを引き継ぐ
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME if FROZEN else ROOT
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "desktop.json"  # インストール先の記録（旧版と共通）
VERSIONS_DIR = DATA_DIR / "versions"
HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MAX_BYTES = 1024 * 1024
HTTPS_PORTS = (443, 8443, 8444, 8445)
PAIR_MAX_ATTEMPTS = 5
WATCH_INTERVAL = 0.5
SSE_HEARTBEAT = 15.0
PAIR_CODE_RE = re.compile(r"\d{6}")

# 配布と更新
REPO = "hiromps/clipbridge"
REPO_URL = f"https://github.com/{REPO}"
RELEASES_URL = f"{REPO_URL}/releases"
UPDATE_URL = f"{REPO_URL}/releases/latest/download/windows.json"
SITE_URL = "https://clipbridge-ashen.vercel.app/"
UPDATE_CHECK_DELAY = 10.0
UPDATE_CHECK_INTERVAL = 6 * 3600.0
MAX_UPDATE_BYTES = 100 * 1024 * 1024

STATIC_FILES = {
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/pc.js": ("pc.js", "text/javascript; charset=utf-8"),
    "/vendor/qrcode.js": ("vendor/qrcode.js", "text/javascript; charset=utf-8"),
    "/icon-180.png": ("icon-180.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/favicon.ico": ("icon-180.png", "image/png"),
}


# ---------------------------------------------------------------------------
# クリップボード
# ---------------------------------------------------------------------------


class ClipboardError(Exception):
    """クリップボード操作の失敗。"""


class WindowsClipboard:
    """Win32 API を ctypes で直接呼ぶ（追加パッケージ不要）。"""

    name = "windows"
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.CountClipboardFormats.restype = ctypes.c_int
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        self._user32 = user32
        self._kernel32 = kernel32

    def sequence(self) -> int:
        """クリップボードが書き換わるたびに増える番号。中身を読まずに変化を検知できる。"""
        return int(self._user32.GetClipboardSequenceNumber())

    def _open(self) -> None:
        for _ in range(20):
            if self._user32.OpenClipboard(None):
                return
            time.sleep(0.025)
        raise ClipboardError("クリップボードが他のアプリに使用中です")

    def read(self) -> tuple[str, str]:
        """(テキスト, 種別) を返す。種別は text / empty / other（画像など）。"""
        self._open()
        try:
            if not self._user32.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
                kind = "empty" if self._user32.CountClipboardFormats() == 0 else "other"
                return "", kind
            handle = self._user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                raise ClipboardError("クリップボードを読み取れません")
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                raise ClipboardError("クリップボードをロックできません")
            try:
                text = self._ctypes.wstring_at(pointer)
            finally:
                self._kernel32.GlobalUnlock(handle)
            return text, ("text" if text else "empty")
        finally:
            self._user32.CloseClipboard()

    def write(self, text: str) -> None:
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = self._kernel32.GlobalAlloc(self.GMEM_MOVEABLE, len(data))
        if not handle:
            raise ClipboardError("メモリを確保できません")
        pointer = self._kernel32.GlobalLock(handle)
        if not pointer:
            self._kernel32.GlobalFree(handle)
            raise ClipboardError("メモリをロックできません")
        self._ctypes.memmove(pointer, data, len(data))
        self._kernel32.GlobalUnlock(handle)
        self._open()
        try:
            if not self._user32.EmptyClipboard() or not self._user32.SetClipboardData(
                self.CF_UNICODETEXT, handle
            ):
                self._kernel32.GlobalFree(handle)
                raise ClipboardError("クリップボードへ書き込めません")
        finally:
            self._user32.CloseClipboard()


class MemoryClipboard:
    """テストおよび Windows 以外の OS 用の仮クリップボード。"""

    name = "memory"

    def __init__(self) -> None:
        self.text = ""
        self.kind = "empty"
        self._sequence = 0

    def sequence(self) -> int:
        return self._sequence

    def read(self) -> tuple[str, str]:
        return self.text, self.kind

    def write(self, text: str) -> None:
        self.text = text
        self.kind = "text" if text else "empty"
        self._sequence += 1

    def external_copy(self, text: str, kind: str = "text") -> None:
        """PC 側で Ctrl+C されたことを模倣する。"""
        self.text = text
        self.kind = kind
        self._sequence += 1


def detect_clipboard():
    if sys.platform == "win32":
        return WindowsClipboard()
    return MemoryClipboard()


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 共有状態
# ---------------------------------------------------------------------------


class ClipState:
    """最新のクリップボード内容と SSE 購読者。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.text = ""
        self.kind = "empty"
        self.hash = text_hash("")
        self.updated = time.time()
        self.source = "pc"
        self._subscribers: set[queue.Queue] = set()

    def _payload(self) -> dict:
        return {
            "text": self.text,
            "kind": self.kind,
            "hash": self.hash,
            "updated": self.updated,
            "source": self.source,
            "length": len(self.text),
        }

    def snapshot(self) -> dict:
        with self._lock:
            return self._payload()

    def set(self, text: str, kind: str, source: str) -> bool:
        """内容が変わったときだけ更新して購読者へ配信する。"""
        with self._lock:
            new_hash = text_hash(text)
            if new_hash == self.hash and kind == self.kind:
                return False
            self.text, self.kind, self.hash = text, kind, new_hash
            self.updated, self.source = time.time(), source
            payload = self._payload()
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(payload)
            except queue.Full:
                pass
        return True

    def subscribe(self) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue(maxsize=32)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


class Pairing:
    """6 桁のペアリングコード。成功または規定回数の失敗で新しいコードに替える。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.code = ""
        self.attempts = 0
        self.issued = 0.0
        self.rotate()

    def _rotate_locked(self) -> None:
        self.code = f"{secrets.randbelow(1_000_000):06d}"
        self.attempts = 0
        self.issued = time.time()

    def rotate(self) -> str:
        with self._lock:
            self._rotate_locked()
            return self.code

    def attempt(self, supplied: str) -> bool:
        if not PAIR_CODE_RE.fullmatch(supplied or ""):
            return False
        with self._lock:
            if hmac.compare_digest(supplied, self.code):
                self._rotate_locked()
                return True
            self.attempts += 1
            if self.attempts >= PAIR_MAX_ATTEMPTS:
                self._rotate_locked()
            return False

    def info(self) -> dict:
        with self._lock:
            return {
                "code": self.code,
                "attempts_left": PAIR_MAX_ATTEMPTS - self.attempts,
                "issued": self.issued,
            }


# ---------------------------------------------------------------------------
# Tailscale
# ---------------------------------------------------------------------------


class Tailscale:
    """tailscale CLI の薄いラッパー。"""

    def __init__(self, exe: str | None = None) -> None:
        self.exe = exe or shutil.which("tailscale") or self._default_path()

    @staticmethod
    def _default_path() -> str | None:
        if sys.platform == "win32":
            for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
                if base and (Path(base) / "Tailscale" / "tailscale.exe").exists():
                    return str(Path(base) / "Tailscale" / "tailscale.exe")
        elif sys.platform == "darwin":
            candidate = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
            if Path(candidate).exists():
                return candidate
        return None

    def available(self) -> bool:
        return bool(self.exe)

    def run(self, *args: str, timeout: float = 10) -> tuple[int, str, str]:
        if not self.exe:
            return 127, "", "tailscale not found"
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
        try:
            completed = subprocess.run(
                [self.exe, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=flags,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, "", str(exc)
        return completed.returncode, completed.stdout, completed.stderr

    def status(self) -> dict | None:
        code, out, _ = self.run("status", "--json")
        if code != 0 or not out.strip():
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None

    def serve_config(self) -> dict | None:
        code, out, _ = self.run("serve", "status", "--json")
        if code != 0:
            return None
        try:
            return json.loads(out) if out.strip() else {}
        except ValueError:
            return {}

    def serve_https(self, https_port: int, local_port: int) -> tuple[bool, str]:
        code, out, err = self.run(
            "serve", "--bg", f"--https={https_port}", f"http://127.0.0.1:{local_port}", timeout=30
        )
        return code == 0, (out + "\n" + err).strip()


def proxy_target_port(proxy: str) -> int | None:
    parts = urlsplit(proxy if "://" in proxy else "http://" + proxy)
    if parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        return None
    try:
        return parts.port
    except ValueError:
        return None


def classify_port(serve: dict | None, dns: str, port: int, local_port: int) -> str:
    """Tailscale Serve の HTTPS ポートが ours / free / taken のどれかを判定する。"""
    serve = serve or {}
    tcp_entry = (serve.get("TCP") or {}).get(str(port))
    handlers = ((serve.get("Web") or {}).get(f"{dns}:{port}") or {}).get("Handlers") or {}
    root = handlers.get("/") or {}
    if root.get("Proxy") and proxy_target_port(root["Proxy"]) == local_port:
        return "ours"
    if tcp_entry is None and not handlers:
        return "free"
    if tcp_entry is not None and not tcp_entry.get("HTTPS"):
        return "taken"
    if not root:
        return "free"
    return "taken"


def choose_https_port(serve: dict | None, dns: str, local_port: int) -> tuple[int | None, str]:
    for port in HTTPS_PORTS:
        state = classify_port(serve, dns, port, local_port)
        if state in ("ours", "free"):
            return port, state
    return None, "taken"


def public_url(dns: str, https_port: int) -> str:
    return f"https://{dns}/" if https_port == 443 else f"https://{dns}:{https_port}/"


def funnel_enabled(serve: dict | None, dns: str, port: int) -> bool:
    return bool(((serve or {}).get("AllowFunnel") or {}).get(f"{dns}:{port}"))


def summarize_tailscale(installed: bool, status: dict | None, serve: dict | None, local_port: int) -> dict:
    """ダッシュボード用のチェック結果にまとめる（純粋関数）。"""
    checks: list[dict] = []

    def add(check_id: str, label: str, state: str, detail: str = "", hint: str = "") -> None:
        checks.append({"id": check_id, "label": label, "state": state, "detail": detail, "hint": hint})

    label_installed = "Tailscale がインストールされている"
    if not installed:
        add(
            "installed", label_installed, "fail", "tailscale コマンドが見つかりません",
            "https://tailscale.com/download から Tailscale をインストールしてログインしてください。",
        )
        return {
            "installed": False, "backend_state": None, "checks": checks, "dns_name": None,
            "https_port": None, "serve_state": "unknown", "public_url": None, "planned_url": None,
            "ready": False,
        }
    add("installed", label_installed, "ok")

    backend = (status or {}).get("BackendState")
    self_info = (status or {}).get("Self") or {}
    online = bool(self_info.get("Online"))
    dns = (self_info.get("DNSName") or "").rstrip(".") or None
    label_running = "Tailscale に接続している"
    if status is None:
        add("running", label_running, "fail", "状態を取得できません", "Tailscale アプリを起動してください。")
    elif backend == "Running" and online:
        ips = ", ".join(self_info.get("TailscaleIPs") or [])
        add("running", label_running, "ok", f"{self_info.get('HostName', '')} {ips}".strip())
    elif backend == "NeedsLogin":
        add("running", label_running, "fail", "ログインが必要です", "Tailscale アプリでログインしてください。")
    else:
        add("running", label_running, "fail", f"状態: {backend}", "Tailscale アプリで接続をオンにしてください。")

    tailnet = (status or {}).get("CurrentTailnet") or {}
    magic = bool(tailnet.get("MagicDNSEnabled")) and bool(dns)
    add(
        "dns", "MagicDNS が有効", "ok" if magic else "fail", dns or "",
        "" if magic else "Tailscale 管理画面の DNS → MagicDNS を有効にしてください: https://login.tailscale.com/admin/dns",
    )
    certs = (status or {}).get("CertDomains") or []
    add(
        "https", "HTTPS 証明書が有効", "ok" if certs else "fail", ", ".join(certs),
        "" if certs else "Tailscale 管理画面の DNS → HTTPS Certificates → Enable HTTPS を押してください: https://login.tailscale.com/admin/dns",
    )

    https_port: int | None = None
    serve_state = "unknown"
    if dns:
        https_port, serve_state = choose_https_port(serve, dns, local_port)
    label_serve = "ClipBridge を HTTPS で公開している（Tailscale Serve）"
    if serve_state == "ours" and https_port:
        add("serve", label_serve, "ok", public_url(dns, https_port))
    elif serve_state == "free" and https_port:
        add("serve", label_serve, "warn", f"未設定（ポート {https_port} を使います）", "「HTTPS 公開を設定する」を押してください。")
    elif serve_state == "taken":
        add("serve", label_serve, "fail", "空いている HTTPS ポートがありません", "tailscale serve status で既存の設定を確認してください。")
    else:
        add("serve", label_serve, "fail", "MagicDNS 名が取得できません", "")

    if dns and https_port:
        if funnel_enabled(serve, dns, https_port):
            add(
                "funnel", "インターネット公開（Funnel）がオフ", "fail",
                "Funnel が有効です。ClipBridge がインターネットに公開されています",
                f"PowerShell で tailscale funnel --https={https_port} off を実行してください。",
            )
        else:
            add("funnel", "インターネット公開（Funnel）がオフ", "ok")

    ready = all(check["state"] == "ok" for check in checks)
    return {
        "installed": True,
        "backend_state": backend,
        "checks": checks,
        "dns_name": dns,
        "https_port": https_port,
        "serve_state": serve_state,
        "public_url": public_url(dns, https_port) if (dns and https_port and serve_state == "ours") else None,
        "planned_url": public_url(dns, https_port) if (dns and https_port) else None,
        "ready": ready,
    }


# ---------------------------------------------------------------------------
# 自動起動（Windows のスタートアップフォルダー）
# ---------------------------------------------------------------------------


def pythonw_executable() -> str:
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def default_startup_dir() -> Path | None:
    if sys.platform != "win32":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_script(startup_dir: Path) -> Path:
    return startup_dir / "ClipBridge.vbs"


def startup_launcher(executable: str | None = None) -> str:
    """exe 版の自動起動で実行するもの。版が変わっても追従するようスタートメニューのショートカットを優先する。"""
    shortcut = start_menu_shortcut()
    if shortcut is not None and shortcut.exists():
        return str(shortcut)
    installed = installed_executable()
    if installed.exists():
        return str(installed)
    return executable or sys.executable


def set_startup(
    enabled: bool,
    startup_dir: Path,
    server_path: Path | None = None,
    python: str | None = None,
    port: int = DEFAULT_PORT,
    frozen: bool = FROZEN,
    launcher: str | None = None,
) -> Path:
    """スタートアップフォルダーに VBS を置く／消す。WSH は ANSI で読むため ASCII のみで書く。"""
    path = startup_script(startup_dir)
    if not enabled:
        if path.exists():
            path.unlink()
        return path

    def quoted(value: str) -> str:
        return '""' + value + '""'

    if frozen:
        command = f"{quoted(launcher or startup_launcher())} --no-browser --port {port}"
    else:
        server_path = server_path or (ROOT / "server.py")
        python = python or pythonw_executable()
        command = f"{quoted(python)} {quoted(str(server_path))} --no-browser --port {port}"
    content = (
        "' ClipBridge autostart (generated by server.py). Delete this file to disable.\r\n"
        f'CreateObject("WScript.Shell").Run "{command}", 0, False\r\n'
    )
    startup_dir.mkdir(parents=True, exist_ok=True)
    # content は CRLF で書いてあるので、newline="" で LF から CRLF への二重変換を止める
    path.write_text(content, encoding="mbcs" if sys.platform == "win32" else "utf-8", newline="")
    return path


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


def load_config(path: Path) -> dict:
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
    changed = False
    token = data.get("token")
    if not isinstance(token, str) or len(token) < 32:
        data["token"] = secrets.token_urlsafe(32)
        changed = True
    if "created" not in data:
        data["created"] = time.time()
        changed = True
    if changed:
        save_config(path, data)
    return data


def save_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def device_name(user_agent: str) -> str:
    if "iPad" in user_agent:
        return "iPad"
    if "iPhone" in user_agent:
        return "iPhone"
    if "Android" in user_agent:
        return "Android"
    if "Macintosh" in user_agent:
        return "Mac"
    if "Windows" in user_agent:
        return "Windows PC"
    return "端末"


# ---------------------------------------------------------------------------
# インストール（exe 版）と再起動
# ---------------------------------------------------------------------------


def read_state(path: Path = STATE_PATH) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(value: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def start_menu_shortcut() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if sys.platform != "win32" or not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "ClipBridge.lnk"


def installed_executable(versions_dir: Path = VERSIONS_DIR) -> Path:
    return versions_dir / __version__ / "ClipBridge.exe"


def is_installed_location(executable: Path, versions_dir: Path = VERSIONS_DIR) -> bool:
    """%LOCALAPPDATA%\\ClipBridge\\versions\\<版>\\ から起動しているか。"""
    try:
        return executable.resolve().parent.parent == versions_dir.resolve()
    except OSError:
        return False


def create_shortcuts(target: Path, runner=subprocess.run) -> None:
    """デスクトップとスタートメニューの ClipBridge.lnk を target に向ける（管理者権限不要）。"""

    def ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    command = (
        "$w=New-Object -ComObject WScript.Shell;"
        "$d=[Environment]::GetFolderPath('Desktop');"
        "$m=Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs';"
        "foreach($p in @($d,$m)){$s=$w.CreateShortcut((Join-Path $p 'ClipBridge.lnk'));"
        f"$s.TargetPath={ps_quote(str(target))};$s.WorkingDirectory={ps_quote(str(target.parent))};"
        "$s.Description='ClipBridge';$s.Save()}"
    )
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
    runner(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        check=True, capture_output=True, timeout=30, creationflags=flags,
    )


def install_for_user(
    executable: Path | None = None,
    versions_dir: Path = VERSIONS_DIR,
    state_path: Path = STATE_PATH,
    runner=subprocess.run,
) -> Path:
    """exe を %LOCALAPPDATA%\\ClipBridge\\versions\\<版>\\ に保存し、ショートカットと記録を更新する。

    旧版（v1）と同じ場所・同じ記録ファイルを使うので、v1 からの更新でもトークンと旧版の exe はそのまま残る。
    """
    source = Path(executable or sys.executable)
    target = installed_executable(versions_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        temp = target.with_suffix(".new")
        shutil.copy2(source, temp)
        try:
            os.replace(temp, target)
        except OSError:
            temp.unlink(missing_ok=True)
            if not target.exists():  # 同じ版が起動中で置き換えられないだけなら、それをそのまま使う
                raise
    state = read_state(state_path)
    previous = state.get("current_executable")
    if previous and previous != str(target) and Path(previous).is_file():
        state["previous_executable"] = previous  # 旧版は消さない（手動で戻せる）
    state["current_executable"] = str(target)
    save_state(state, state_path)
    try:
        create_shortcuts(target, runner)
    except (OSError, subprocess.SubprocessError):
        pass  # ショートカットは任意。本体の保存は済んでいる
    return target


def wait_for_process(pid: int, timeout: float = 30.0) -> bool:
    """指定 PID のプロセスが終了するまで待つ（更新・再起動の引き継ぎ用）。"""
    if pid <= 0 or pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return True  # すでに終了している
        try:
            return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def spawn_detached(command: list[str], cwd: Path | None = None) -> None:
    """自分とは独立したプロセスとして起動する（親が終了しても残る）。"""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        creationflags=flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def restart_command(argv: list[str], wait_pid: int, frozen: bool = FROZEN, executable: str | None = None) -> list[str]:
    """同じ設定で自分を起動し直すコマンド。旧プロセスの終了を待ち、ブラウザーは開かない。"""
    kept: list[str] = []
    skip = False
    for arg in argv[1:]:
        if skip:
            skip = False
            continue
        if arg == "--wait-pid":
            skip = True
            continue
        if arg.startswith("--wait-pid=") or arg in ("--install", "--minimized", "--no-browser"):
            continue
        kept.append(arg)
    launcher = [executable or sys.executable]
    if not frozen:
        launcher.append(str(Path(__file__).resolve()))
    return launcher + kept + ["--no-browser", "--wait-pid", str(wait_pid)]


# ---------------------------------------------------------------------------
# アップデート（exe 版: GitHub Releases / ソース版: git pull）
# ---------------------------------------------------------------------------

VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def version_tuple(value) -> tuple[int, ...]:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise ValueError("バージョン情報が不正です")
    return tuple(int(part) for part in value.split("."))


def validate_manifest(value) -> dict:
    """更新情報は公式 GitHub Releases の ClipBridge.exe だけを受け付ける（旧版と同じ検証）。"""
    if not isinstance(value, dict):
        raise ValueError("更新情報の形式が不正です")
    version_tuple(value.get("version"))
    url = urlsplit(str(value.get("url", "")))
    expected = f"/{REPO}/releases/download/v{value['version']}/ClipBridge.exe"
    if (
        url.scheme != "https" or url.hostname != "github.com" or url.path != expected
        or url.query or url.fragment or url.username
    ):
        raise ValueError("更新ファイルの配布元を確認できません")
    if not re.fullmatch(r"[a-f0-9]{64}", str(value.get("sha256", ""))):
        raise ValueError("更新ファイルの検証情報が不正です")
    if type(value.get("size")) is not int or not 0 < value["size"] <= MAX_UPDATE_BYTES:
        raise ValueError("更新ファイルのサイズが不正です")
    return value


def fetch_manifest(url: str = UPDATE_URL, opener=urllib.request.urlopen) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": f"{APP_NAME}/{__version__}", "Cache-Control": "no-cache"}
    )
    try:
        with opener(request, timeout=15) as response:
            raw = response.read(65537)
    except OSError as exc:
        raise RuntimeError(f"更新情報を取得できません（{type(exc).__name__}）。通信を確認してください") from exc
    if len(raw) > 65536:
        raise ValueError("更新情報が大きすぎます")
    try:
        return validate_manifest(json.loads(raw.decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"更新情報を読めません: {exc}") from exc


def download_update(manifest: dict, directory: Path, opener=urllib.request.urlopen) -> Path:
    """exe をダウンロードしてサイズと SHA-256 を検証する。失敗したら消す。"""
    manifest = validate_manifest(manifest)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "ClipBridge.exe"
    digest = hashlib.sha256()
    size = 0
    try:
        request = urllib.request.Request(manifest["url"], headers={"User-Agent": f"{APP_NAME}/{__version__}"})
        with opener(request, timeout=60) as response, target.open("wb") as output:
            if urlsplit(getattr(response, "url", manifest["url"])).scheme != "https":
                raise ValueError("安全な接続でダウンロードできません")
            while chunk := response.read(256 * 1024):
                size += len(chunk)
                if size > manifest["size"]:
                    raise ValueError("更新ファイルのサイズが一致しません")
                digest.update(chunk)
                output.write(chunk)
        if size != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
            raise ValueError("更新ファイルの検証に失敗しました。現在のアプリは変更していません")
        with target.open("rb") as file:
            if file.read(2) != b"MZ":
                raise ValueError("Windows アプリの形式が不正です")
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise


def find_git_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents[:2]):
        if (candidate / ".git").exists():
            return candidate
    return None


class GitUpdater:
    """git clone したソース版の更新（git fetch / pull --ff-only）。"""

    def __init__(self, root: Path = ROOT, runner=None) -> None:
        self.repo = find_git_root(root)
        self.git = shutil.which("git")
        self._runner = runner

    def available(self) -> bool:
        return self.repo is not None and (self.git is not None or self._runner is not None)

    def run(self, *args: str, timeout: float = 90) -> tuple[int, str, str]:
        if self._runner is not None:
            return self._runner(list(args))
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
        try:
            completed = subprocess.run(
                [self.git, "-C", str(self.repo), *args],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, creationflags=flags,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, "", str(exc)
        return completed.returncode, completed.stdout, completed.stderr

    def _out(self, *args: str, timeout: float = 90) -> str:
        code, out, err = self.run(*args, timeout=timeout)
        if code != 0:
            raise RuntimeError(f"git {args[0]} に失敗しました: {(err or out).strip()[:300]}")
        return out.strip()

    def check(self) -> dict:
        branch = self._out("rev-parse", "--abbrev-ref", "HEAD") or "main"
        self._out("fetch", "--quiet", "origin", branch)
        behind = int(self._out("rev-list", "--count", f"HEAD..origin/{branch}") or 0)
        return {
            "branch": branch,
            "behind": behind,
            "current": self._out("log", "-1", "--format=%h %cs", "HEAD"),
            "latest": self._out("log", "-1", "--format=%h %cs", f"origin/{branch}"),
            "dirty": bool(self._out("status", "--porcelain", "--untracked-files=no")),
        }

    def apply(self, branch: str) -> str:
        if self._out("status", "--porcelain", "--untracked-files=no"):
            raise RuntimeError("ローカルに未コミットの変更があるため更新を中止しました")
        return self._out("pull", "--ff-only", "origin", branch, timeout=180)


class Updater:
    """更新の確認と適用。exe 版は GitHub Releases、git clone 版は git pull、それ以外は案内のみ。"""

    def __init__(self, app: ClipBridgeApp, *, fetch=fetch_manifest, git: GitUpdater | None = None, frozen: bool = FROZEN) -> None:
        self.app = app
        self.fetch = fetch
        self.git = git if git is not None else GitUpdater(ROOT)
        if frozen:
            self.method = "exe"
        elif self.git.available():
            self.method = "git"
        else:
            self.method = "source"
        self._lock = threading.Lock()
        self._manifest: dict | None = None
        self._branch = "main"
        self.applying = False
        self.info: dict = {
            "method": self.method,
            "current": __version__,
            "latest": None,
            "available": False,
            "can_apply": self.method in ("exe", "git"),
            "checked_at": None,
            "error": None,
            "detail": "",
            "releases_url": RELEASES_URL,
            "site_url": SITE_URL,
        }

    def check(self) -> dict:
        info = {**self.info, "error": None}
        try:
            if self.method == "git":
                result = self.git.check()
                self._branch = result["branch"]
                info["current"] = f"{__version__} ({result['current']})"
                info["latest"] = result["latest"]
                info["available"] = result["behind"] > 0
                info["dirty"] = result["dirty"]
                info["detail"] = f"{result['behind']} 件の変更" if result["behind"] else "最新です"
            else:
                manifest = self.fetch()
                self._manifest = manifest
                info["latest"] = manifest["version"]
                info["available"] = version_tuple(manifest["version"]) > version_tuple(__version__)
                info["detail"] = f"{manifest['size'] / 1_000_000:.1f} MB" if info["available"] else "最新です"
        except Exception as exc:  # noqa: BLE001 - 原因を画面に出す（秘密情報は含まれない）
            info["error"] = str(exc) or type(exc).__name__
            info["available"] = False
        info["checked_at"] = time.time()
        with self._lock:
            self.info = info
        self.app.log(f"update check ({info['method']}): " + ("error" if info["error"] else f"available={info['available']}"))
        return info

    def apply(self) -> dict:
        """更新を取得して適用し、再起動を予約する。失敗したら例外（現在の状態は変えない）。"""
        if self.applying:
            raise RuntimeError("更新を適用しています。しばらくお待ちください")
        info = self.check()
        if info["error"]:
            raise RuntimeError(info["error"])
        if not info["available"]:
            raise RuntimeError("すでに最新です")
        self.applying = True
        try:
            if self.method == "git":
                self.git.apply(self._branch)
                command = restart_command(sys.argv, os.getpid())
            elif self.method == "exe" and self._manifest:
                path = download_update(self._manifest, Path(tempfile.mkdtemp(prefix="ClipBridge-update-")))
                command = [str(path), "--install", "--wait-pid", str(os.getpid()), "--no-browser"]
            else:
                raise RuntimeError(f"この版は自動更新できません。{RELEASES_URL} から取得してください")
        except Exception:
            self.applying = False
            raise
        self.app.restart_with(command)
        return {"ok": True, "message": "更新を適用して再起動しています…", "version": info["latest"]}


def self_test(output: Path) -> None:
    """同梱ファイルとサーバー起動を確認して結果を JSON に書く（利用者のクリップボードや設定には触れない）。"""
    with tempfile.TemporaryDirectory(prefix="ClipBridge-self-test-") as sandbox:
        app = ClipBridgeApp(
            port=0, config_path=Path(sandbox) / "config.json", clipboard=MemoryClipboard(),
            startup_dir=Path(sandbox) / "Startup",
        )
        httpd = app.serve()
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://{HOST}:{app.port}"
            with urllib.request.urlopen(base + "/api/health", timeout=5) as response:
                if json.load(response).get("app") != APP_NAME:
                    raise RuntimeError("health check failed")
            for path in ("/", "/style.css", "/app.js", "/pc.js", "/vendor/qrcode.js", "/icon-180.png", "/icon-512.png"):
                with urllib.request.urlopen(base + path, timeout=5) as response:
                    if response.status != 200 or not response.read():
                        raise RuntimeError(f"missing {path}")
        finally:
            app.stopping = True
            httpd.shutdown()
            httpd.server_close()
    output.write_text(
        json.dumps({"ok": True, "version": __version__, "frozen": FROZEN, "clipboard": detect_clipboard().name}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# アプリ本体
# ---------------------------------------------------------------------------


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows の SO_REUSEADDR は別プロセスとの二重バインドを許してしまうため無効にする
    allow_reuse_address = sys.platform != "win32"


class ClipBridgeApp:
    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        config_path: Path = CONFIG_PATH,
        clipboard=None,
        tailscale: Tailscale | None = None,
        startup_dir: Path | None = None,
        watch_interval: float = WATCH_INTERVAL,
        web_dir: Path = WEB_DIR,
        updater: Updater | None = None,
    ) -> None:
        self.port = port
        self.config_path = config_path
        self.config = load_config(config_path)
        self.token: str = self.config["token"]
        self.pc_name = self.config.get("pc_name") or socket.gethostname()
        self.clipboard = clipboard or detect_clipboard()
        self._clip_lock = threading.Lock()
        self.state = ClipState()
        self.pairing = Pairing()
        self.devices: dict[str, dict] = {}
        self._devices_lock = threading.Lock()
        self.tailscale = tailscale or Tailscale()
        self._ts_lock = threading.Lock()
        self._ts_cache: tuple[float, dict | None] = (0.0, None)
        self.serve_result: dict | None = None
        self.stopping = False
        self.started = time.time()
        self.server: Server | None = None
        self.startup_dir = startup_dir if startup_dir is not None else default_startup_dir()
        self.watch_interval = watch_interval
        self.web_dir = web_dir
        self.restart_command: list[str] | None = None
        self.updater = updater or Updater(self)
        self._refresh_from_clipboard(initial=True)

    # --- ログ -------------------------------------------------------------
    def log(self, message: str) -> None:
        if sys.stdout is None:
            return
        try:
            print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)
        except (OSError, UnicodeEncodeError):
            pass

    # --- クリップボード -----------------------------------------------------
    def _refresh_from_clipboard(self, initial: bool = False) -> None:
        try:
            with self._clip_lock:
                text, kind = self.clipboard.read()
        except Exception as exc:  # noqa: BLE001 - 内容はログに出さない
            if initial:
                self.log(f"clipboard read failed: {type(exc).__name__}")
            return
        self.state.set(normalize_newlines(text), kind, "pc")

    def start_watcher(self) -> threading.Thread:
        thread = threading.Thread(target=self._watch_loop, name="clipboard-watch", daemon=True)
        thread.start()
        return thread

    def _watch_loop(self) -> None:
        last_sequence = None
        while not self.stopping:
            try:
                sequence = self.clipboard.sequence()
                if sequence != last_sequence:
                    last_sequence = sequence
                    self._refresh_from_clipboard()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(self.watch_interval)

    def write_clipboard(self, text: str) -> dict:
        with self._clip_lock:
            self.clipboard.write(text)
        normalized = normalize_newlines(text)
        self.state.set(normalized, "text" if normalized else "empty", "phone")
        return self.state.snapshot()

    # --- Tailscale ---------------------------------------------------------
    def tailscale_summary(self, force: bool = False) -> dict:
        with self._ts_lock:
            cached_at, cached = self._ts_cache
            if not force and cached is not None and time.time() - cached_at < 3:
                return cached
        installed = self.tailscale.available()
        status = self.tailscale.status() if installed else None
        serve = self.tailscale.serve_config() if installed else None
        summary = summarize_tailscale(installed, status, serve, self.port)
        with self._ts_lock:
            self._ts_cache = (time.time(), summary)
        return summary

    def ensure_serve(self) -> dict:
        """Tailscale Serve で HTTPS 公開を設定する（他サービスの設定は上書きしない）。"""
        summary = self.tailscale_summary(force=True)
        if not summary["installed"]:
            result = {"ok": False, "message": "Tailscale が見つかりません。インストールしてログインしてください。"}
        elif summary["backend_state"] != "Running" or not summary["dns_name"]:
            result = {"ok": False, "message": "Tailscale に接続してから「再確認」を押してください。"}
        elif summary["serve_state"] == "ours":
            result = {"ok": True, "message": f"HTTPS 公開は設定済みです: {summary['public_url']}"}
        elif summary["serve_state"] == "free" and summary["https_port"]:
            ok, output = self.tailscale.serve_https(summary["https_port"], self.port)
            summary = self.tailscale_summary(force=True)
            if ok and summary["serve_state"] == "ours":
                result = {"ok": True, "message": f"HTTPS 公開を設定しました: {summary['public_url']}"}
            else:
                hint = ""
                if "denied" in output.lower() or "拒否" in output:
                    hint = (
                        " 管理者として開いた PowerShell で次を実行してください: "
                        f"tailscale serve --bg --https={summary['https_port']} http://127.0.0.1:{self.port}"
                    )
                result = {"ok": False, "message": f"設定に失敗しました: {output or '原因不明'}{hint}"}
        else:
            result = {"ok": False, "message": "空いている HTTPS ポートがありません。tailscale serve status を確認してください。"}
        result["at"] = time.time()
        self.serve_result = result
        self.log(f"tailscale serve: {'ok' if result['ok'] else 'failed'}")
        return result

    # --- 端末記録 -----------------------------------------------------------
    def record_device(self, handler: BaseHTTPRequestHandler) -> None:
        user_agent = handler.headers.get("User-Agent", "")
        forwarded = handler.headers.get("X-Forwarded-For", "")
        ip = (forwarded.split(",")[0].strip() if forwarded else handler.client_address[0])
        name = device_name(user_agent)
        key = f"{ip}|{name}"
        with self._devices_lock:
            self.devices[key] = {
                "name": name,
                "ip": ip,
                "user": handler.headers.get("Tailscale-User-Login", ""),
                "last_seen": time.time(),
            }
            if len(self.devices) > 10:
                oldest = min(self.devices, key=lambda k: self.devices[k]["last_seen"])
                del self.devices[oldest]

    def device_list(self) -> list[dict]:
        with self._devices_lock:
            return sorted(self.devices.values(), key=lambda d: d["last_seen"], reverse=True)

    # --- 管理 ---------------------------------------------------------------
    def rotate_token(self) -> None:
        self.config["token"] = secrets.token_urlsafe(32)
        save_config(self.config_path, self.config)
        self.token = self.config["token"]
        with self._devices_lock:
            self.devices.clear()
        self.pairing.rotate()
        self.log("token rotated")

    def token_matches(self, supplied: str | None) -> bool:
        if not isinstance(supplied, str) or not supplied:
            return False
        return hmac.compare_digest(supplied.encode("utf-8"), self.token.encode("utf-8"))

    def admin_status(self) -> dict:
        tailscale = self.tailscale_summary()
        pair = self.pairing.info()
        url = tailscale.get("public_url")
        snapshot = self.state.snapshot()
        startup_path = startup_script(self.startup_dir) if self.startup_dir else None
        return {
            "app": APP_NAME,
            "version": __version__,
            "pc_name": self.pc_name,
            "port": self.port,
            "started": self.started,
            "now": time.time(),
            "pair_code": pair["code"],
            "pair_attempts_left": pair["attempts_left"],
            "public_url": url,
            "planned_url": tailscale.get("planned_url"),
            "pair_url": f"{url}?code={pair['code']}" if url else None,
            "tailscale": tailscale,
            "serve_result": self.serve_result,
            "devices": self.device_list(),
            "subscribers": self.state.subscriber_count(),
            "startup": {
                "supported": startup_path is not None,
                "enabled": bool(startup_path and startup_path.exists()),
                "path": str(startup_path) if startup_path else None,
            },
            "clipboard_backend": self.clipboard.name,
            "clip": {"kind": snapshot["kind"], "length": snapshot["length"], "updated": snapshot["updated"]},
            "config_path": str(self.config_path),
            "update": self.updater.info,
            "frozen": FROZEN,
            "install_path": str(installed_executable()) if FROZEN else None,
            "site_url": SITE_URL,
        }

    def request_shutdown(self) -> None:
        self.stopping = True
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def restart_with(self, command: list[str]) -> None:
        """サーバーを止めたあとに command を起動する（更新・再起動用）。実際の起動は main() が行う。"""
        self.restart_command = command
        self.log("restart scheduled")
        self.request_shutdown()

    def start_update_checker(self, delay: float = UPDATE_CHECK_DELAY, interval: float = UPDATE_CHECK_INTERVAL) -> threading.Thread:
        """起動直後と一定間隔で更新を確認する（結果は画面に表示するだけで、適用は利用者の操作による）。"""

        def loop() -> None:
            wait = delay
            while not self.stopping:
                deadline = time.time() + wait
                while not self.stopping and time.time() < deadline:
                    time.sleep(0.5)
                if self.stopping:
                    return
                try:
                    self.updater.check()
                except Exception:  # noqa: BLE001
                    pass
                wait = interval

        thread = threading.Thread(target=loop, name="update-check", daemon=True)
        thread.start()
        return thread

    # --- サーバー -----------------------------------------------------------
    def serve(self) -> Server:
        self.server = Server((HOST, self.port), make_handler(self))
        self.port = self.server.server_address[1]
        return self.server


# ---------------------------------------------------------------------------
# HTTP ハンドラ
# ---------------------------------------------------------------------------


def make_handler(app: ClipBridgeApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"{APP_NAME}/{__version__}"
        sys_version = ""

        # ログにはクエリ・本文・トークンを一切含めない
        def log_request(self, code="-", size="-") -> None:  # type: ignore[override]
            app.log(f"{self.command} {urlsplit(self.path).path} {code}")

        def log_error(self, format: str, *args) -> None:  # type: ignore[override]
            app.log("http error: " + (format % args))

        def log_message(self, format: str, *args) -> None:  # type: ignore[override]
            app.log(format % args)

        # --- 送信ヘルパー ---------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload, content_type: str = "application/json; charset=utf-8") -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), content_type)

        def _error(self, status: int, code: str, message: str) -> None:
            self._json(status, {"error": code, "message": message})

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _file(self, name: str, content_type: str) -> None:
            path = app.web_dir / name
            if not path.is_file():
                return self._error(404, "not_found", "ファイルがありません")
            self._send(200, path.read_bytes(), content_type)

        def _not_found(self) -> None:
            self._error(404, "not_found", "見つかりません")

        # --- 判定ヘルパー ---------------------------------------------------
        def _query(self) -> dict[str, str]:
            parsed = parse_qs(urlsplit(self.path).query, keep_blank_values=False)
            return {key: values[0] for key, values in parsed.items() if values}

        def _supplied_token(self) -> str | None:
            return self.headers.get("X-Token") or self._query().get("t")

        def _require_token(self) -> bool:
            if app.token_matches(self._supplied_token()):
                return True
            self._error(401, "unauthorized", "PC と接続し直してください")
            return False

        def _host_matches(self, origin: str) -> bool:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https"):
                return False
            hosts = {
                (self.headers.get("Host") or "").lower(),
                (self.headers.get("X-Forwarded-Host") or "").lower(),
            }
            hosts.discard("")
            return parsed.netloc.lower() in hosts

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return origin is None or self._host_matches(origin)

        def _is_admin(self) -> bool:
            """PC 自身のブラウザーから直接（プロキシ経由でなく）来たリクエストか。"""
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return False
            for header in ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Tailscale-User-Login"):
                if header in self.headers:
                    return False
            host = (self.headers.get("Host") or "").lower()
            return host in {f"127.0.0.1:{app.port}", f"localhost:{app.port}", f"[::1]:{app.port}"}

        def _admin_post_ok(self) -> bool:
            if self.headers.get("X-ClipBridge") != "1":
                return False
            if not self._origin_ok():
                return False
            fetch_site = self.headers.get("Sec-Fetch-Site")
            return fetch_site in (None, "same-origin", "none")

        def _read_body(self, limit: int) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                self._error(411, "length_required", "Content-Length が必要です")
                return None
            if length < 0:
                self._error(400, "bad_request", "Content-Length が不正です")
                return None
            if length > limit:
                # 送信側がレスポンスを受け取れるよう、上限ぶんだけ読み捨ててから閉じる
                self.rfile.read(min(length, limit + 1))
                self.close_connection = True
                self._error(413, "too_large", "1MB を超えています")
                return None
            return self.rfile.read(length)

        # --- GET ----------------------------------------------------------
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            query = self._query()

            if path == "/":
                return self._serve_index(query)
            if path == "/manifest.webmanifest":
                return self._serve_manifest(query)
            if path in STATIC_FILES:
                return self._file(*STATIC_FILES[path])
            if path.startswith("/apple-touch-icon") and path.endswith(".png"):
                # iOS Safari が慣例的に探しに来るサイズ違いのアイコン名をまとめて返す
                return self._file("icon-180.png", "image/png")
            if path == "/api/health":
                return self._json(200, {"ok": True, "app": APP_NAME, "version": __version__})
            if path == "/api/clip":
                if not self._require_token():
                    return
                app.record_device(self)
                return self._json(200, app.state.snapshot())
            if path == "/api/events":
                if not self._require_token():
                    return
                app.record_device(self)
                return self._serve_events()
            if path.startswith("/pc"):
                if not self._is_admin():
                    return self._not_found()
                if path == "/pc":
                    return self._file("pc.html", "text/html; charset=utf-8")
                if path == "/pc/api/status":
                    return self._json(200, app.admin_status())
                if path == "/pc/client":
                    return self._redirect(f"/?t={app.token}")
            return self._not_found()

        do_HEAD = do_GET

        def _serve_index(self, query: dict[str, str]) -> None:
            if self._is_admin() and not query:
                return self._redirect("/pc")
            code = query.get("code")
            if code:
                if app.pairing.attempt(code):
                    app.log("pairing ok (link)")
                    app.record_device(self)
                    return self._redirect(f"/?t={app.token}")
                return self._redirect("/?pair=invalid")
            token = query.get("t")
            if token and not app.token_matches(token):
                return self._redirect("/?pair=invalid")
            manifest = "/manifest.webmanifest" + (f"?t={token}" if token else "")
            template = (app.web_dir / "index.html").read_text(encoding="utf-8")
            page = template.replace("__MANIFEST_HREF__", html.escape(manifest, quote=True))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

        def _serve_manifest(self, query: dict[str, str]) -> None:
            token = query.get("t")
            start_url = f"/?t={token}" if app.token_matches(token) else "/"
            manifest = {
                "name": APP_NAME,
                "short_name": APP_NAME,
                "lang": "ja",
                "id": "/",
                "start_url": start_url,
                "scope": "/",
                "display": "standalone",
                "background_color": "#f4f6fa",
                "theme_color": "#2563eb",
                "icons": [
                    {"src": "/icon-180.png", "sizes": "180x180", "type": "image/png"},
                    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                ],
            }
            self._json(200, manifest, content_type="application/manifest+json; charset=utf-8")

        def _serve_events(self) -> None:
            subscriber = app.state.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"retry: 3000\n\n")
                self._sse_event("clip", app.state.snapshot())
                while not app.stopping:
                    try:
                        payload = subscriber.get(timeout=SSE_HEARTBEAT)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        continue
                    self._sse_event("clip", payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                app.state.unsubscribe(subscriber)

        def _sse_event(self, name: str, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False)
            self.wfile.write(f"event: {name}\ndata: {data}\n\n".encode("utf-8"))

        # --- POST ---------------------------------------------------------
        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/pair":
                return self._pair()
            if path == "/api/clip":
                return self._write_clip()
            if path.startswith("/pc/api/"):
                return self._admin_action(path[len("/pc/api/"):])
            if path in ("/", "/api/health", "/api/events", "/manifest.webmanifest") or path in STATIC_FILES:
                return self._error(405, "method_not_allowed", "使用できないメソッドです")
            return self._not_found()

        def _pair(self) -> None:
            body = self._read_body(4096)
            if body is None:
                return
            try:
                data = json.loads(body.decode("utf-8") or "{}")
                code = str(data.get("code", "")).strip()
            except (ValueError, AttributeError):
                return self._error(400, "bad_request", "JSON が不正です")
            if not PAIR_CODE_RE.fullmatch(code):
                return self._error(400, "bad_code", "6 桁の数字を入力してください")
            if not app.pairing.attempt(code):
                app.log("pairing failed")
                return self._error(401, "wrong_code", "コードが違います。PC の画面に表示されている最新のコードを入力してください")
            app.log("pairing ok")
            app.record_device(self)
            self._json(200, {"token": app.token, "pc_name": app.pc_name})

        def _write_clip(self) -> None:
            if not self._require_token():
                return
            if not self._origin_ok():
                return self._error(403, "origin_mismatch", "Origin が一致しません")
            content_type = (self.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("text/plain"):
                return self._error(415, "unsupported_media_type", "text/plain; charset=utf-8 で送ってください")
            body = self._read_body(MAX_BYTES)
            if body is None:
                return
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                return self._error(400, "bad_encoding", "UTF-8 のテキストが必要です")
            try:
                snapshot = app.write_clipboard(text)
            except Exception as exc:  # noqa: BLE001 - 内容は出さない
                app.log(f"clipboard write failed: {type(exc).__name__}")
                return self._error(500, "clipboard", "PC のクリップボードへ書き込めませんでした")
            app.record_device(self)
            app.log(f"clipboard written: {len(text)} chars")
            self._json(200, {"ok": True, "length": len(text), "hash": snapshot["hash"]})

        def _admin_action(self, action: str) -> None:
            if not self._is_admin():
                return self._not_found()
            if not self._admin_post_ok():
                return self._error(403, "forbidden", "この操作は PC の ClipBridge 画面からのみ行えます")
            body = self._read_body(4096)
            if body is None:
                return
            try:
                data = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                return self._error(400, "bad_request", "JSON が不正です")
            if action == "serve":
                return self._json(200, app.ensure_serve())
            if action == "pair-code":
                code = app.pairing.rotate()
                return self._json(200, {"ok": True, "pair_code": code})
            if action == "token/rotate":
                app.rotate_token()
                return self._json(200, {"ok": True})
            if action == "startup":
                if app.startup_dir is None:
                    return self._error(400, "unsupported", "自動起動の設定は Windows でのみ使えます")
                enabled = bool(data.get("enabled"))
                try:
                    path = set_startup(enabled, app.startup_dir, port=app.port)
                except OSError as exc:
                    return self._error(500, "startup_failed", f"自動起動の設定に失敗しました: {type(exc).__name__}")
                app.log(f"startup {'enabled' if enabled else 'disabled'}")
                return self._json(200, {"ok": True, "enabled": enabled, "path": str(path)})
            if action == "update/check":
                return self._json(200, app.updater.check())
            if action == "update/apply":
                try:
                    result = app.updater.apply()
                except (RuntimeError, ValueError, OSError) as exc:
                    app.log(f"update failed: {type(exc).__name__}")
                    return self._error(400, "update_failed", str(exc) or type(exc).__name__)
                app.log("update applied, restarting")
                return self._json(200, result)
            if action == "shutdown":
                self._json(200, {"ok": True})
                app.log("shutdown requested")
                app.request_shutdown()
                return
            return self._not_found()

        def _method_not_allowed(self) -> None:
            self._error(405, "method_not_allowed", "使用できないメソッドです")

        do_PUT = _method_not_allowed
        do_DELETE = _method_not_allowed
        do_PATCH = _method_not_allowed

    return Handler


# ---------------------------------------------------------------------------
# 起動
# ---------------------------------------------------------------------------


def running_instance(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=1) as response:
            return json.loads(response.read()).get("app") == APP_NAME
    except Exception:  # noqa: BLE001
        return False


def notify(message: str) -> None:
    """コンソールがあれば表示し、exe 版（ウィンドウなし）ならメッセージボックスで知らせる。"""
    if sys.stdout is not None:
        try:
            print(message, flush=True)
            return
        except (OSError, UnicodeEncodeError):
            pass
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)  # MB_ICONINFORMATION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ClipBridge: iPhone と Windows のクリップボード共有サーバー")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"待ち受けポート（既定 {DEFAULT_PORT}）")
    parser.add_argument("--no-browser", action="store_true", help="起動時にブラウザーを開かない")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="設定ファイルの場所")
    parser.add_argument("--startup", choices=("on", "off"), help="Windows サインイン時の自動起動を登録/解除して終了")
    parser.add_argument("--no-update-check", action="store_true", help="起動時と定期の更新確認をしない")
    parser.add_argument("--no-serve", action="store_true", help="Tailscale Serve の自動設定をしない（開発・テスト用）")
    parser.add_argument("--wait-pid", type=int, metavar="PID", help="指定したプロセスの終了を待ってから起動する（更新・再起動用）")
    parser.add_argument("--install", action="store_true", help="exe 版: アプリを %%LOCALAPPDATA%%\\ClipBridge に保存してショートカットを作る")
    # 旧版（v1）の自動起動スクリプトが渡す引数。--no-browser と同じ扱い
    parser.add_argument("--minimized", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test", type=Path, metavar="OUTPUT", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.minimized:
        args.no_browser = True

    if args.self_test:
        self_test(args.self_test)
        return 0

    if args.startup:
        startup_dir = default_startup_dir()
        if startup_dir is None:
            notify("自動起動の登録は Windows でのみ使えます。")
            return 1
        path = set_startup(args.startup == "on", startup_dir, port=args.port)
        notify(("自動起動を登録しました: " if args.startup == "on" else "自動起動を解除しました: ") + str(path))
        return 0

    if args.wait_pid:
        wait_for_process(args.wait_pid)

    if FROZEN and (args.install or not is_installed_location(Path(sys.executable))):
        # ZIP から直接起動されたとき、または更新の引き継ぎ: %LOCALAPPDATA% に保存して、保存先から起動し直す
        target = install_for_user()
        if Path(sys.executable).resolve() != target.resolve():
            command = [str(target)]
            if args.no_browser:
                command.append("--no-browser")
            if args.port != DEFAULT_PORT:
                command += ["--port", str(args.port)]
            if args.no_update_check:
                command.append("--no-update-check")
            spawn_detached(command, cwd=target.parent)
            return 0

    dashboard = f"http://{HOST}:{args.port}/pc"
    if running_instance(args.port):
        if not args.no_browser:
            webbrowser.open(dashboard)
        elif sys.stdout is not None:
            notify(f"{APP_NAME} は既に起動しています: {dashboard}")
        return 0

    app = ClipBridgeApp(port=args.port, config_path=args.config)
    try:
        server = app.serve()
    except OSError as exc:
        notify(f"ポート {args.port} で待ち受けできません: {exc}")
        return 1
    app.start_watcher()
    if not args.no_serve:
        threading.Thread(target=app.ensure_serve, name="tailscale-serve", daemon=True).start()
    if not args.no_update_check:
        app.start_update_checker()
    app.log(f"{APP_NAME} {__version__} 起動: {dashboard}")
    app.log("iPhone 用の接続情報（QR / コード）はこの画面に表示されます。")
    if app.clipboard.name != "windows":
        app.log("注意: この OS では実際のクリップボードと連携しません（開発用）。")
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, [dashboard]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stopping = True
        server.server_close()
    if app.restart_command:
        try:
            spawn_detached(app.restart_command, cwd=ROOT)
        except OSError as exc:
            notify(f"再起動に失敗しました: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
