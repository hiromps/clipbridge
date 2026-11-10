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
  - 画像（iPhone → PC）: POST /api/image で届いた画像を GDI+ でビットマップにして PC のクリップボードに入れる
    （Ctrl+V で貼り付けできる）。?save=1 なら「ピクチャ\\ClipBridge」にも保存する。形式は中身で判定する
  - 画像（PC → iPhone）: PC のクリップボードが画像になったら PNG にして保持し、SSE でメタ情報を配る。
    iPhone は GET /api/image で本体を取り、表示・コピー・写真への保存ができる
  - 複数 PC: iPhone は 1 台目の PC の画面（ハブ）から他の PC の API を直接呼ぶ。そのため /api/* は
    同じ tailnet のオリジンからのクロスオリジン呼び出し（CORS）を許し、/api/peers で他の PC を探せる
  - クリップボードの中身・トークン・コードはログに出さない
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
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
import struct
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

__version__ = "2.5.0"
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
MAX_IMAGE_BYTES = 30 * 1024 * 1024  # iPhone の写真（HEIC → JPEG 変換後で数 MB）に十分な上限
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
# iPhone 用ショートカット（コピーした文字をアプリを開かずに PC へ送る）。iPhone 側の初回セットアップで案内する
SHORTCUT_URL = "https://www.icloud.com/shortcuts/36c11823bdb7479b9fb2c47e0c917beb"
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
    CF_BITMAP = 2
    CF_DIB = 8
    CF_UNICODETEXT = 13
    CF_HDROP = 15
    GMEM_MOVEABLE = 0x0002
    PROPERTY_TAG_ORIENTATION = 0x0112

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._gdiplus = None
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        self._gdi32 = gdi32
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
        kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        shell32.DragQueryFileW.restype = wintypes.UINT
        self._user32 = user32
        self._kernel32 = kernel32
        self._shell32 = shell32

    def sequence(self) -> int:
        """クリップボードが書き換わるたびに増える番号。中身を読まずに変化を検知できる。"""
        return int(self._user32.GetClipboardSequenceNumber())

    def _open(self) -> None:
        for _ in range(20):
            if self._user32.OpenClipboard(None):
                return
            time.sleep(0.025)
        raise ClipboardError("クリップボードが他のアプリに使用中です")

    def _dropped_image(self) -> Path | None:
        """エクスプローラーなどでコピーされたファイル（CF_HDROP）の先頭が画像なら、そのパスを返す。クリップボードは開いた状態で呼ぶ。"""
        if not self._user32.IsClipboardFormatAvailable(self.CF_HDROP):
            return None
        handle = self._user32.GetClipboardData(self.CF_HDROP)
        if not handle or self._shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0) < 1:
            return None
        buffer = self._ctypes.create_unicode_buffer(32768)
        if not self._shell32.DragQueryFileW(handle, 0, buffer, 32768):
            return None
        path = Path(buffer.value)
        try:
            if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
                return None
            with path.open("rb") as file:
                head = file.read(64)
        except OSError:
            return None
        return path if sniff_image(head) else None

    def read(self) -> tuple[str, str]:
        """(テキスト, 種別) を返す。種別は text / empty / image / other（画像以外のファイルなど）。"""
        self._open()
        try:
            if not self._user32.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
                if self._user32.CountClipboardFormats() == 0:
                    kind = "empty"
                elif self._user32.IsClipboardFormatAvailable(self.CF_DIB) or self._user32.IsClipboardFormatAvailable(self.CF_BITMAP):
                    kind = "image"
                elif self._dropped_image() is not None:
                    kind = "image"  # 画像ファイルのコピーも画像として扱う
                else:
                    kind = "other"
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

    # --- 画像（GDI+ を ctypes で直接呼ぶ。追加パッケージ不要） ---------------------
    def _gdip(self):
        """GDI+ を初回だけ起動して関数の型を設定する。終了処理はプロセス終了に任せる。"""
        if self._gdiplus is not None:
            return self._gdiplus
        ctypes, wintypes = self._ctypes, self._wintypes

        class StartupInput(ctypes.Structure):
            _fields_ = [
                ("GdiplusVersion", ctypes.c_uint32),
                ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wintypes.BOOL),
                ("SuppressExternalCodecs", wintypes.BOOL),
            ]

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

        gdiplus = ctypes.WinDLL("gdiplus", use_last_error=True)
        gdiplus.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(StartupInput), ctypes.c_void_p]
        gdiplus.GdiplusStartup.restype = ctypes.c_int
        gdiplus.GdipCreateBitmapFromFile.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        gdiplus.GdipCreateBitmapFromFile.restype = ctypes.c_int
        gdiplus.GdipCreateBitmapFromHBITMAP.argtypes = [wintypes.HBITMAP, wintypes.HPALETTE, ctypes.POINTER(ctypes.c_void_p)]
        gdiplus.GdipCreateBitmapFromHBITMAP.restype = ctypes.c_int
        gdiplus.GdipCreateHBITMAPFromBitmap.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.HBITMAP), ctypes.c_uint32]
        gdiplus.GdipCreateHBITMAPFromBitmap.restype = ctypes.c_int
        gdiplus.GdipSaveImageToFile.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(GUID), ctypes.c_void_p]
        gdiplus.GdipSaveImageToFile.restype = ctypes.c_int
        gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
        gdiplus.GdipDisposeImage.restype = ctypes.c_int
        gdiplus.GdipGetPropertyItemSize.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        gdiplus.GdipGetPropertyItemSize.restype = ctypes.c_int
        gdiplus.GdipGetPropertyItem.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        gdiplus.GdipGetPropertyItem.restype = ctypes.c_int
        gdiplus.GdipImageRotateFlip.argtypes = [ctypes.c_void_p, ctypes.c_int]
        gdiplus.GdipImageRotateFlip.restype = ctypes.c_int
        self._gdip_token = ctypes.c_size_t()
        startup = StartupInput(1, None, False, False)
        status = gdiplus.GdiplusStartup(ctypes.byref(self._gdip_token), ctypes.byref(startup), None)
        if status != 0:
            raise ClipboardError(f"画像ライブラリ (GDI+) を初期化できません ({status})")
        # PNG エンコーダーの CLSID {557CF406-1A04-11D3-9A73-0000F81EF32E}
        self._png_clsid = GUID(0x557CF406, 0x1A04, 0x11D3, (ctypes.c_ubyte * 8)(0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E))
        self._user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        self._user32.RegisterClipboardFormatW.restype = wintypes.UINT
        self._png_format = int(self._user32.RegisterClipboardFormatW("PNG"))  # Chrome / GIMP などが使う PNG そのままの形式
        self._gdiplus = gdiplus
        return gdiplus

    @staticmethod
    def _temp_file(data: bytes | None, suffix: str) -> Path:
        handle, name = tempfile.mkstemp(prefix="clipbridge-", suffix=suffix)
        with os.fdopen(handle, "wb") as file:
            if data:
                file.write(data)
        return Path(name)

    def _global_copy(self, data: bytes):
        """GMEM_MOVEABLE なグローバルメモリに data を複製する（クリップボードに渡すと所有権が移る）。"""
        handle = self._kernel32.GlobalAlloc(self.GMEM_MOVEABLE, len(data))
        if not handle:
            raise ClipboardError("メモリを確保できません")
        pointer = self._kernel32.GlobalLock(handle)
        if not pointer:
            self._kernel32.GlobalFree(handle)
            raise ClipboardError("メモリをロックできません")
        self._ctypes.memmove(pointer, data, len(data))
        self._kernel32.GlobalUnlock(handle)
        return handle

    def _exif_orientation(self, gdiplus, bitmap) -> int:
        """EXIF の Orientation（1〜8）。無ければ 1。iPhone の写真は回転情報だけで向きを表すことが多い。"""
        ctypes = self._ctypes

        class PropertyItem(ctypes.Structure):
            _fields_ = [("id", ctypes.c_uint32), ("length", ctypes.c_uint32), ("type", ctypes.c_uint16), ("value", ctypes.c_void_p)]

        size = ctypes.c_uint32()
        if gdiplus.GdipGetPropertyItemSize(bitmap, self.PROPERTY_TAG_ORIENTATION, ctypes.byref(size)) != 0:
            return 1
        if size.value < ctypes.sizeof(PropertyItem):
            return 1
        buffer = ctypes.create_string_buffer(size.value)
        if gdiplus.GdipGetPropertyItem(bitmap, self.PROPERTY_TAG_ORIENTATION, size.value, buffer) != 0:
            return 1
        item = PropertyItem.from_buffer(buffer)
        if item.type != 3 or item.length < 2 or not item.value:  # 3 = PropertyTagTypeShort
            return 1
        value = ctypes.cast(item.value, ctypes.POINTER(ctypes.c_uint16))[0]
        return value if 1 <= value <= 8 else 1

    def write_image(self, data: bytes) -> None:
        """画像（ファイルの中身）を CF_BITMAP としてクリップボードに入れる（CF_DIB は Windows が自動変換する）。

        PNG のときは PNG 形式（透過を保てる）も併せて入れる。GDI+ はファイルからしか開けないので一時ファイルを経由する。
        """
        ctypes, wintypes = self._ctypes, self._wintypes
        gdiplus = self._gdip()
        path = self._temp_file(data, ".img")
        bitmap = ctypes.c_void_p()
        hbitmap = wintypes.HBITMAP()
        try:
            status = gdiplus.GdipCreateBitmapFromFile(str(path), ctypes.byref(bitmap))
            if status != 0 or not bitmap:
                raise ClipboardError(f"この形式の画像は Windows で開けません (GDI+ {status})")
            flip = ROTATE_FLIP_FOR_EXIF.get(self._exif_orientation(gdiplus, bitmap), 0)
            if flip:
                gdiplus.GdipImageRotateFlip(bitmap, flip)
            status = gdiplus.GdipCreateHBITMAPFromBitmap(bitmap, ctypes.byref(hbitmap), 0xFFFFFFFF)  # 透明部分は白
            if status != 0 or not hbitmap:
                raise ClipboardError(f"画像をビットマップに変換できません (GDI+ {status})")
        finally:
            if bitmap:
                gdiplus.GdipDisposeImage(bitmap)
            path.unlink(missing_ok=True)
        png_handle = self._global_copy(data) if data.startswith(PNG_SIGNATURE) else None
        self._open()
        try:
            if not self._user32.EmptyClipboard() or not self._user32.SetClipboardData(self.CF_BITMAP, hbitmap):
                self._gdi32.DeleteObject(hbitmap)
                if png_handle:
                    self._kernel32.GlobalFree(png_handle)
                raise ClipboardError("クリップボードへ書き込めません")
            if png_handle and not self._user32.SetClipboardData(self._png_format, png_handle):
                self._kernel32.GlobalFree(png_handle)  # ビットマップは入っているので続行
        finally:
            self._user32.CloseClipboard()

    def read_image(self) -> bytes:
        """クリップボードの画像を返す。PNG 形式があればそのまま、ビットマップは GDI+ で PNG に、画像ファイルのコピーはファイルの中身をそのまま。"""
        ctypes = self._ctypes
        gdiplus = self._gdip()
        bitmap = ctypes.c_void_p()
        self._open()
        try:
            if self._user32.IsClipboardFormatAvailable(self._png_format):
                handle = self._user32.GetClipboardData(self._png_format)
                pointer = self._kernel32.GlobalLock(handle) if handle else None
                if pointer:
                    try:
                        data = ctypes.string_at(pointer, self._kernel32.GlobalSize(handle))
                    finally:
                        self._kernel32.GlobalUnlock(handle)
                    if data.startswith(PNG_SIGNATURE):
                        return data
            if not self._user32.IsClipboardFormatAvailable(self.CF_BITMAP):
                dropped = self._dropped_image()
                if dropped is None:
                    raise ClipboardError("クリップボードに画像がありません")
                try:
                    return dropped.read_bytes()
                except OSError as exc:
                    raise ClipboardError(f"コピーされた画像ファイルを読めません: {type(exc).__name__}") from exc
            hbitmap = self._user32.GetClipboardData(self.CF_BITMAP)
            if not hbitmap:
                raise ClipboardError("クリップボードの画像を読み取れません")
            status = gdiplus.GdipCreateBitmapFromHBITMAP(hbitmap, None, ctypes.byref(bitmap))  # 画素はここで複製される
            if status != 0 or not bitmap:
                raise ClipboardError(f"画像を読み取れません (GDI+ {status})")
        finally:
            self._user32.CloseClipboard()
        path = self._temp_file(None, ".png")
        try:
            status = gdiplus.GdipSaveImageToFile(bitmap, str(path), ctypes.byref(self._png_clsid), None)
            if status != 0:
                raise ClipboardError(f"PNG に変換できません (GDI+ {status})")
            return path.read_bytes()
        finally:
            gdiplus.GdipDisposeImage(bitmap)
            path.unlink(missing_ok=True)


# EXIF Orientation → GDI+ RotateFlipType（Rotate90FlipNone=1, Rotate180FlipNone=2, Rotate270FlipNone=3,
# RotateNoneFlipX=4, Rotate90FlipX=5, Rotate180FlipX=6, Rotate270FlipX=7）
ROTATE_FLIP_FOR_EXIF = {1: 0, 2: 4, 3: 2, 4: 6, 5: 5, 6: 1, 7: 7, 8: 3}


class MemoryClipboard:
    """テストおよび Windows 以外の OS 用の仮クリップボード。"""

    name = "memory"

    def __init__(self) -> None:
        self.text = ""
        self.kind = "empty"
        self.image_data: bytes | None = None
        self._sequence = 0

    def sequence(self) -> int:
        return self._sequence

    def read(self) -> tuple[str, str]:
        return self.text, self.kind

    def write(self, text: str) -> None:
        self.text = text
        self.kind = "text" if text else "empty"
        self.image_data = None
        self._sequence += 1

    def write_image(self, data: bytes) -> None:
        self.text = ""
        self.kind = "image"
        self.image_data = data
        self._sequence += 1

    def read_image(self) -> bytes:
        if self.image_data is None:
            raise ClipboardError("クリップボードに画像がありません")
        return self.image_data

    def external_copy(self, text: str, kind: str = "text") -> None:
        """PC 側で Ctrl+C されたことを模倣する。"""
        self.text = text
        self.kind = kind
        self.image_data = None
        self._sequence += 1

    def external_copy_image(self, data: bytes) -> None:
        """PC 側で画像をコピー（スクリーンショットなど）したことを模倣する。"""
        self.text = ""
        self.kind = "image"
        self.image_data = data
        self._sequence += 1

    def external_copy_file(self, path: Path) -> None:
        """PC 側でエクスプローラーのファイルをコピーしたことを模倣する（画像ファイルなら画像、それ以外は other）。"""
        data = path.read_bytes()
        self.text = ""
        if sniff_image(data):
            self.kind, self.image_data = "image", data
        else:
            self.kind, self.image_data = "other", None
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
# 画像ファイル（iPhone → PC）
# ---------------------------------------------------------------------------

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
IMAGE_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (PNG_SIGNATURE, "png", "png"),
    (b"\xff\xd8\xff", "jpeg", "jpg"),
    (b"GIF87a", "gif", "gif"),
    (b"GIF89a", "gif", "gif"),
    (b"BM", "bmp", "bmp"),
)
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """先頭バイトから画像形式を判定して (種別, 拡張子) を返す。Content-Type やファイル名は信用しない。"""
    for signature, kind, ext in IMAGE_SIGNATURES:
        if data.startswith(signature) and len(data) > 16:
            return kind, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "webp"
    if len(data) > 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in HEIF_BRANDS:
            return "heic", "heic"
        if brand in (b"avif", b"avis"):
            return "avif", "avif"
    return None


IMAGE_CONTENT_TYPES = {
    "png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "bmp": "image/bmp",
    "webp": "image/webp", "heic": "image/heic", "avif": "image/avif",
}


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """PNG / GIF / JPEG の幅と高さ（iPhone 側の表示用。取れなければ None）。"""
    if data.startswith(PNG_SIGNATURE) and len(data) >= 24 and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data.startswith(b"\xff\xd8"):
        position = 2
        while position + 9 < len(data):
            if data[position] != 0xFF:
                position += 1
                continue
            marker = data[position + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                position += 2
                continue
            length = struct.unpack(">H", data[position + 2:position + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[position + 5:position + 9])
                return width, height
            position += 2 + length
    return None


def safe_stem(name: str) -> str:
    """送られてきたファイル名から、Windows でそのまま使える部分だけを残す（パス区切り・記号は捨てる）。"""
    stem = Path(name.replace("\\", "/")).stem
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_ ") else "_" for ch in stem).strip(" _")
    return cleaned[:60] or "image"


def image_filename(original: str, ext: str, now: time.struct_time | None = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", now or time.localtime())
    return f"{stamp}-{safe_stem(original)}.{ext}"


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, suffix = candidate.stem, candidate.suffix
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def pictures_dir() -> Path:
    """Windows の「ピクチャ」フォルダー（OneDrive に移動されていてもその場所）。取れなければ ~/Pictures。"""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

            folder_id = GUID(0x33E28130, 0x4E1E, 0x4676, (ctypes.c_ubyte * 8)(0x83, 0x5A, 0x98, 0x39, 0x5C, 0x3B, 0xC3, 0xBB))
            shell32 = ctypes.WinDLL("shell32")
            ole32 = ctypes.WinDLL("ole32")
            shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
            shell32.SHGetKnownFolderPath.restype = ctypes.c_long
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            result = ctypes.c_wchar_p()
            if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(result)) == 0 and result.value:
                value = result.value
                ole32.CoTaskMemFree(result)
                return Path(value)
        except (OSError, AttributeError, ValueError):
            pass
    return Path.home() / "Pictures"


def default_image_dir() -> Path:
    return pictures_dir() / APP_NAME


def open_folder(path: Path) -> None:
    """エクスプローラー（または OS 既定のファイルマネージャー）でフォルダーを開く。"""
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---------------------------------------------------------------------------
# Pro ライセンス（オフライン検証: RSA-2048 / PKCS#1 v1.5 / SHA-256。秘密鍵は販売者だけが持つ）
# ---------------------------------------------------------------------------

# scripts/license_tool.py keygen の出力（公開鍵）。秘密鍵はリポジトリに入れない
LICENSE_PUBLIC_KEY: dict = {
    "n": (
        "9b29755fdd813be92ae1e724775dd1a08d79693a697950ac1159d068d49a4616aa92c2bf70c26b6e3894e08636a656a19426870af8d16b6de63eeafd3c11de2c"
        "d530c6b7d3dd91bba4fded1201a9fe844fcedadda6505c300fc7b85a87c6c9a585a3698903523cce95d230ae3609139d7c27aaa23c224ac4012a55ab7cad9a39"
        "81fc29edc4651a34a3f53816265f64aefd2590e79af3f4c7df9824a3e397e9e057097920085aa66648d98d66fe2d08b76c1837aef984d672093c550fcc8d778c"
        "c2acd190127e5587911c4a2db5787babc7328b0a05e02357c42c100e6cba8b760a6c61b78bbdbdaa24781071ddef67095e0aacc10428c73f11dfb9c47d5be3ad"
    ),
    "e": 65537,
}
LICENSE_PREFIX = "CBP1."
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
PRO_FEATURES = ("unlimited_pcs", "history", "image_rules", "hotkeys", "support")
SUPPORT_EMAIL = ""  # 優先サポートの宛先。空なら GitHub Issues だけを案内する
SUPPORT_URL = f"{REPO_URL}/issues/new?labels=priority-support"


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def rsa_verify(message: bytes, signature: bytes, public: dict) -> bool:
    """RSASSA-PKCS1-v1_5（SHA-256）の検証。標準ライブラリの pow だけで済ませる。"""
    try:
        n = int(public["n"], 16) if isinstance(public["n"], str) else int(public["n"])
        e = int(public["e"])
    except (KeyError, TypeError, ValueError):
        return False
    size = (n.bit_length() + 7) // 8
    if n <= 0 or len(signature) != size:
        return False
    decrypted = pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big")
    digest_info = SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
    return hmac.compare_digest(decrypted, expected)


def parse_license(key: str | None, public: dict | None = None, now: float | None = None) -> dict:
    """ライセンスキー（CBP1.<payload>.<signature>）を検証して内容を返す。valid=False のときは error に理由。"""
    public = public or LICENSE_PUBLIC_KEY
    result: dict = {
        "valid": False, "error": None, "id": None, "email": None, "name": None,
        "plan": None, "issued": None, "expires": None,
    }
    key = (key or "").strip()
    if not key:
        result["error"] = "ライセンスキーが入力されていません"
        return result
    if not key.startswith(LICENSE_PREFIX) or key.count(".") != 2:
        result["error"] = "ライセンスキーの形式が違います（CBP1. で始まる 3 区切りの文字列です）"
        return result
    try:
        _, payload_b64, signature_b64 = key.split(".")
        payload = _b64url_decode(payload_b64)
        signature = _b64url_decode(signature_b64)
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        result["error"] = "ライセンスキーを読み取れません"
        return result
    if not isinstance(data, dict) or data.get("product") != "clipbridge":
        result["error"] = "この製品のライセンスではありません"
        return result
    if not public.get("n") or not rsa_verify(payload, signature, public):
        result["error"] = "署名を確認できません（キーが改変されているか、別の発行元です）"
        return result
    for field in ("id", "email", "name", "plan", "issued", "expires"):
        value = data.get(field)
        result[field] = value if isinstance(value, str) or value is None else str(value)
    if result["expires"]:
        try:
            deadline = time.mktime(time.strptime(result["expires"], "%Y-%m-%d")) + 86400
        except (ValueError, OverflowError):
            result["error"] = "有効期限の形式が不正です"
            return result
        if (now if now is not None else time.time()) > deadline:
            result["error"] = f"ライセンスの有効期限（{result['expires']}）が切れています"
            return result
    if result["plan"] != "pro":
        result["error"] = "Pro のライセンスではありません"
        return result
    result["valid"] = True
    return result


# ---------------------------------------------------------------------------
# Pro: クリップボード履歴（直近 100 件 + ピン留め。JSON と画像ファイルに保存）
# ---------------------------------------------------------------------------

HISTORY_LIMIT = 100
HISTORY_PREVIEW = 400
HISTORY_ID_RE = re.compile(r"[0-9a-f]{12}")


class ClipHistory:
    """履歴。entries は新しい順。ピン留めは上限に数えず、消去でも残す。"""

    def __init__(self, path: Path, limit: int = HISTORY_LIMIT) -> None:
        self.path = path
        self.image_dir = path.with_name(path.stem)  # history.json → history/
        self.limit = limit
        self._lock = threading.Lock()
        self.entries: list[dict] = []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.entries = [entry for entry in data if isinstance(entry, dict) and entry.get("id")]
        except (OSError, ValueError):
            self.entries = []

    # --- 内部 ---
    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.entries, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.path)

    def _remove_file(self, entry: dict) -> None:
        if entry.get("kind") == "image" and entry.get("file"):
            try:
                (self.image_dir / entry["file"]).unlink(missing_ok=True)
            except OSError:
                pass

    def _evict(self) -> None:
        kept: list[dict] = []
        dropped: list[dict] = []
        count = 0
        for entry in self.entries:
            if entry.get("pinned"):
                kept.append(entry)
                continue
            count += 1
            (kept if count <= self.limit else dropped).append(entry)
        self.entries = kept
        for entry in dropped:
            if not any(other.get("file") == entry.get("file") for other in kept if entry.get("file")):
                self._remove_file(entry)

    @staticmethod
    def _public(entry: dict, full: bool = False) -> dict:
        item = {key: value for key, value in entry.items() if key != "text"}
        if entry.get("kind") == "text":
            text = entry.get("text", "")
            item["text"] = text if full or len(text) <= HISTORY_PREVIEW else text[:HISTORY_PREVIEW]
            item["truncated"] = not full and len(text) > HISTORY_PREVIEW
        return item

    def _insert(self, entry: dict, digest: str) -> dict:
        previous = next((item for item in self.entries if item.get("hash") == digest), None)
        if previous is not None:
            entry["pinned"] = bool(previous.get("pinned"))
            self.entries.remove(previous)
        self.entries.insert(0, entry)
        self._evict()
        self._save()
        return self._public(entry)

    # --- 追加 ---
    def add_text(self, text: str, source: str) -> dict | None:
        text = normalize_newlines(text)
        if not text.strip():
            return None
        with self._lock:
            entry = {
                "id": secrets.token_hex(6), "kind": "text", "hash": text_hash(text), "text": text,
                "length": len(text), "created": time.time(), "source": source, "pinned": False,
            }
            return self._insert(entry, entry["hash"])

    def add_image(self, data: bytes, info: dict | None, source: str) -> dict | None:
        if not data or not info:
            return None
        with self._lock:
            self.image_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{info['id']}.{info['ext']}"
            (self.image_dir / filename).write_bytes(data)
            entry = {
                "id": secrets.token_hex(6), "kind": "image", "hash": "img:" + info["id"], "file": filename,
                "size": info["size"], "width": info["width"], "height": info["height"], "type": info["type"],
                "created": time.time(), "source": source, "pinned": False,
            }
            return self._insert(entry, entry["hash"])

    # --- 参照 ---
    def list(self, query: str = "", limit: int = HISTORY_LIMIT) -> list[dict]:
        needle = query.strip().lower()
        with self._lock:
            items = []
            for entry in self.entries:
                if needle:
                    if entry.get("kind") == "text" and needle in entry.get("text", "").lower():
                        pass
                    elif entry.get("kind") == "image" and needle in ("画像", "image", "img"):
                        pass
                    else:
                        continue
                items.append(self._public(entry))
                if len(items) >= limit:
                    break
            return items

    def get(self, entry_id: str) -> dict | None:
        with self._lock:
            return next((entry for entry in self.entries if entry.get("id") == entry_id), None)

    def public(self, entry_id: str) -> dict | None:
        entry = self.get(entry_id)
        return self._public(entry, full=True) if entry else None

    def image_bytes(self, entry_id: str) -> tuple[bytes, dict] | None:
        entry = self.get(entry_id)
        if not entry or entry.get("kind") != "image":
            return None
        try:
            return (self.image_dir / entry["file"]).read_bytes(), entry
        except OSError:
            return None

    def counts(self) -> dict:
        with self._lock:
            return {"total": len(self.entries), "pinned": sum(1 for entry in self.entries if entry.get("pinned"))}

    # --- 変更 ---
    def set_pinned(self, entry_id: str, pinned: bool) -> dict | None:
        with self._lock:
            entry = next((item for item in self.entries if item.get("id") == entry_id), None)
            if entry is None:
                return None
            entry["pinned"] = bool(pinned)
            self._evict()
            self._save()
            return self._public(entry)

    def remove(self, entry_id: str) -> bool:
        with self._lock:
            entry = next((item for item in self.entries if item.get("id") == entry_id), None)
            if entry is None:
                return False
            self.entries.remove(entry)
            if not any(other.get("file") == entry.get("file") for other in self.entries if entry.get("file")):
                self._remove_file(entry)
            self._save()
            return True

    def clear(self, keep_pinned: bool = True) -> int:
        with self._lock:
            removed = [entry for entry in self.entries if not (keep_pinned and entry.get("pinned"))]
            self.entries = [entry for entry in self.entries if keep_pinned and entry.get("pinned")]
            for entry in removed:
                if not any(other.get("file") == entry.get("file") for other in self.entries if entry.get("file")):
                    self._remove_file(entry)
            self._save()
            return len(removed)


# ---------------------------------------------------------------------------
# Pro: 画像の保存ルール（サブフォルダーとファイル名のテンプレート）
# ---------------------------------------------------------------------------

IMAGE_SUBFOLDERS = {"none": "", "date": "%Y-%m-%d", "month": "%Y-%m"}
DEFAULT_IMAGE_TEMPLATE = "{date}-{time}-{name}"
IMAGE_TEMPLATE_TOKENS = ("date", "time", "datetime", "year", "month", "day", "name", "pc")
TEMPLATE_TOKEN_RE = re.compile(r"\{(\w+)\}")


def validate_image_template(template: str) -> str | None:
    """テンプレートの問題点を返す（問題なければ None）。"""
    if not isinstance(template, str) or not template.strip():
        return "ファイル名のテンプレートを入力してください"
    tokens = set(TEMPLATE_TOKEN_RE.findall(template))
    unknown = tokens - set(IMAGE_TEMPLATE_TOKENS)
    if unknown:
        return "使えない項目です: " + ", ".join("{" + token + "}" for token in sorted(unknown))
    if not tokens:
        return "{date} や {name} のような項目を 1 つ以上入れてください"
    return None


def render_image_name(template: str, original: str, ext: str, pc_name: str = "", now: time.struct_time | None = None) -> str:
    """テンプレートからファイル名を作る。Windows で使えない文字は _ に置き換える。"""
    now = now or time.localtime()
    values = {
        "date": time.strftime("%Y%m%d", now), "time": time.strftime("%H%M%S", now),
        "datetime": time.strftime("%Y%m%d-%H%M%S", now),
        "year": time.strftime("%Y", now), "month": time.strftime("%m", now), "day": time.strftime("%d", now),
        "name": safe_stem(original), "pc": safe_stem(pc_name) if pc_name else "pc",
    }
    stem = TEMPLATE_TOKEN_RE.sub(lambda match: values.get(match.group(1), ""), template)
    stem = "".join(ch if (ch.isalnum() or ch in "-_ .") else "_" for ch in stem).strip(" ._")
    return f"{stem[:120] or 'image'}.{ext}"


def image_subfolder(rule: str, now: time.struct_time | None = None) -> str:
    pattern = IMAGE_SUBFOLDERS.get(rule or "none", "")
    return time.strftime(pattern, now or time.localtime()) if pattern else ""


# ---------------------------------------------------------------------------
# Pro: PC 側のホットキー（Windows の RegisterHotKey）
# ---------------------------------------------------------------------------

HOTKEY_ACTIONS = {
    "open_dashboard": "PC セットアップ画面を開く",
    "restore_phone": "iPhone から最後に届いた内容を PC のクリップボードに戻す",
    "restore_previous": "履歴の 1 つ前の内容をクリップボードに戻す（押すたびに遡る）",
}
DEFAULT_HOTKEYS = {"open_dashboard": "Ctrl+Alt+C", "restore_phone": "Ctrl+Alt+V", "restore_previous": "Ctrl+Alt+Left"}
HOTKEY_MODIFIERS = {"ctrl": 0x0002, "control": 0x0002, "alt": 0x0001, "shift": 0x0004, "win": 0x0008}
HOTKEY_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "pause": 0x13, "printscreen": 0x2C, "scrolllock": 0x91,
    "numlock": 0x90, "capslock": 0x14,
}
MOD_NOREPEAT = 0x4000


def parse_hotkey(text: str) -> tuple[int, int]:
    """'Ctrl+Alt+V' のような表記を RegisterHotKey の (修飾キー, 仮想キー) にする。"""
    parts = [part.strip() for part in (text or "").split("+") if part.strip()]
    if not parts:
        raise ValueError("Ctrl+Alt+V のように、修飾キーと通常のキーを + でつないでください")
    modifiers = 0
    key: str | None = None
    for part in parts:
        lowered = part.lower()
        if lowered in HOTKEY_MODIFIERS:
            modifiers |= HOTKEY_MODIFIERS[lowered]
            continue
        if key is not None:
            raise ValueError("通常のキーは 1 つだけにしてください")
        key = lowered
    if key is None:
        raise ValueError("Ctrl や Alt のほかに、通常のキー（文字・数字・F キーなど）を 1 つ指定してください")
    if modifiers == 0:
        raise ValueError("Ctrl / Alt / Shift / Win のいずれかを組み合わせてください")
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())
    elif key in HOTKEY_KEYS:
        vk = HOTKEY_KEYS[key]
    elif re.fullmatch(r"f([1-9]|1\d|2[0-4])", key):
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"使えないキーです: {part}")
    return modifiers | MOD_NOREPEAT, vk


class HotkeyService:
    """グローバルホットキー。登録と受信は専用スレッド（メッセージループ）で行う。Windows 以外では何もしない。"""

    supported = sys.platform == "win32"

    def __init__(self, handler) -> None:
        self.handler = handler
        self.bindings: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, bindings: dict[str, str]) -> dict[str, str]:
        """bindings（action → 'Ctrl+Alt+V'）を登録し、登録できなかった action の理由を返す。"""
        self.stop()
        self.bindings = dict(bindings)
        self.errors = {}
        if not self.supported or not bindings:
            return {}
        ready = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(dict(bindings), ready), name="hotkeys", daemon=True)
        self._thread.start()
        ready.wait(3)
        return dict(self.errors)

    def stop(self) -> None:
        if self._thread is not None and self._thread_id and self.supported:
            import ctypes

            ctypes.WinDLL("user32").PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
            self._thread.join(2)
        self._thread = None
        self._thread_id = None

    def _loop(self, bindings: dict[str, str], ready: threading.Event) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32")
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        self._thread_id = kernel32.GetCurrentThreadId()
        registered: dict[int, str] = {}
        for index, (action, text) in enumerate(bindings.items(), start=1):
            try:
                modifiers, vk = parse_hotkey(text)
            except ValueError as exc:
                self.errors[action] = str(exc)
                continue
            if not user32.RegisterHotKey(None, index, modifiers, vk):
                code = ctypes.get_last_error()
                self.errors[action] = "このキーは他のアプリが使っています" if code == 1409 else f"登録できません（エラー {code}）"
                continue
            registered[index] = action
        ready.set()
        try:
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312:  # WM_HOTKEY
                    action = registered.get(int(message.wParam))
                    if action:
                        try:
                            self.handler(action)
                        except Exception:  # noqa: BLE001 - ホットキーの処理失敗でループを止めない
                            pass
        finally:
            for index in registered:
                user32.UnregisterHotKey(None, index)


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
        self.image: dict | None = None  # kind == "image" のときのメタ情報（id / size / width / height / type）
        self.via: str | None = None  # iPhone からの経路: app（画面）/ shortcut（ショートカット）
        self._subscribers: set[queue.Queue] = set()

    def _payload(self) -> dict:
        return {
            "text": self.text,
            "kind": self.kind,
            "hash": self.hash,
            "updated": self.updated,
            "source": self.source,
            "length": len(self.text),
            "image": self.image,
            "via": self.via,
        }

    def snapshot(self) -> dict:
        with self._lock:
            return self._payload()

    def set(self, text: str, kind: str, source: str, image: dict | None = None, via: str | None = None) -> bool:
        """内容が変わったときだけ更新して購読者へ配信する。"""
        with self._lock:
            image = image if kind == "image" else None
            new_hash = f"image:{image['id']}" if image else text_hash(text)
            if new_hash == self.hash and kind == self.kind:
                return False
            self.text, self.kind, self.hash, self.image = text, kind, new_hash, image
            self.via = via if source == "phone" else None
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


def tailnet_peers(status: dict | None) -> list[dict]:
    """tailscale status の Peer 一覧から、ClipBridge が動いていそうな候補（オンラインの PC）を抜き出す。"""
    peers: list[dict] = []
    for peer in ((status or {}).get("Peer") or {}).values():
        dns = (peer.get("DNSName") or "").rstrip(".")
        if not dns or not peer.get("Online"):
            continue
        os_name = (peer.get("OS") or "").lower()
        if os_name and os_name not in ("windows", "linux", "macos", "darwin"):
            continue  # iPhone / Android などは対象外
        peers.append({"host": peer.get("HostName") or dns.split(".")[0], "dns": dns, "os": os_name})
    return peers


def probe_clipbridge(url: str, opener=urllib.request.urlopen, timeout: float = 2.0) -> dict | None:
    """url（https://pc.tailnet.ts.net/ など）で ClipBridge が応答するか確認し、health の内容を返す。"""
    request = urllib.request.Request(url + "api/health", headers={"User-Agent": f"{APP_NAME}/{__version__}"})
    try:
        with opener(request, timeout=timeout) as response:
            data = json.loads(response.read(4096).decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("app") == APP_NAME else None


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
    if "Shortcuts" in user_agent:
        return "iPhone（ショートカット）"
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
        probe=None,
        image_dir: Path | None = None,
        history_path: Path | None = None,
    ) -> None:
        self.port = port
        self.config_path = config_path
        self.config = load_config(config_path)
        self.token: str = self.config["token"]
        self.pc_name = self.config.get("pc_name") or socket.gethostname()
        configured_dir = self.config.get("image_dir")
        self.image_dir = image_dir or (Path(configured_dir) if isinstance(configured_dir, str) and configured_dir else default_image_dir())
        self.last_image: dict | None = None
        # Pro
        self.license = parse_license(self.config.get("license") if isinstance(self.config.get("license"), str) else None)
        self.history = ClipHistory(history_path or config_path.with_name("history.json"))
        self.history_enabled = bool(self.config.get("history_enabled", True))
        self.image_rules = {
            "subfolder": self.config.get("image_subfolder") if self.config.get("image_subfolder") in IMAGE_SUBFOLDERS else "none",
            "template": self.config.get("image_template") if validate_image_template(self.config.get("image_template") or "") is None else DEFAULT_IMAGE_TEMPLATE,
        }
        self.hotkeys_enabled = bool(self.config.get("hotkeys_enabled", True))
        saved_hotkeys = self.config.get("hotkeys") if isinstance(self.config.get("hotkeys"), dict) else {}
        self.hotkey_bindings = {action: str(saved_hotkeys.get(action) or default) for action, default in DEFAULT_HOTKEYS.items()}
        self.hotkeys = HotkeyService(self.hotkey_action)
        self._cycle_index = 0  # restore_previous で履歴をどこまで遡ったか
        self.clipboard = clipboard or detect_clipboard()
        self._clip_lock = threading.Lock()
        self._own_sequence = -1  # 自分がクリップボードに書いた直後の番号。監視スレッドが読み戻さないようにする
        self._image_lock = threading.Lock()
        self._image: tuple[bytes, dict] | None = None  # PC のクリップボードにある画像（iPhone へ配る本体とメタ情報）
        self.state = ClipState()
        self.pairing = Pairing()
        self.devices: dict[str, dict] = {}
        self._devices_lock = threading.Lock()
        self.tailscale = tailscale or Tailscale()
        self._ts_lock = threading.Lock()
        self._ts_cache: tuple[float, dict | None] = (0.0, None)
        self._ts_status: dict | None = None
        self._suffix_cache: tuple[float, str | None] = (0.0, None)
        self._peers_lock = threading.Lock()
        self._peers_cache: tuple[float, dict | None] = (0.0, None)
        self.probe = probe or probe_clipbridge
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
                image_data: bytes | None = None
                if kind == "image":
                    try:
                        image_data = self.clipboard.read_image()
                    except Exception as exc:  # noqa: BLE001 - プレビューなしで「画像」とだけ伝える
                        self.log(f"clipboard image read failed: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - 内容はログに出さない
            if initial:
                self.log(f"clipboard read failed: {type(exc).__name__}")
            return
        info = self._store_image(image_data)
        if self.state.set(normalize_newlines(text), kind, "pc", info) and not initial:
            self._cycle_index = 0
            if kind == "text":
                self.record_text(text, "pc")
            elif kind == "image" and image_data:
                self.record_image(image_data, info, "pc")

    # --- Pro ------------------------------------------------------------------
    @property
    def pro(self) -> bool:
        return bool(self.license.get("valid"))

    def license_status(self) -> dict:
        return {
            **self.license,
            "pro": self.pro,
            "features": list(PRO_FEATURES),
            "support_url": SUPPORT_URL,
            "support_email": SUPPORT_EMAIL,
        }

    def apply_license(self, key: str) -> dict:
        info = parse_license(key)
        if not info["valid"]:
            raise ValueError(info["error"] or "ライセンスキーが無効です")
        self.config["license"] = key.strip()
        save_config(self.config_path, self.config)
        self.license = info
        self.log(f"license applied: {info['id']}")
        self.start_hotkeys()
        return self.license_status()

    def remove_license(self) -> None:
        self.config.pop("license", None)
        save_config(self.config_path, self.config)
        self.license = parse_license(None)
        self.hotkeys.stop()
        self.log("license removed")

    def record_text(self, text: str, source: str) -> None:
        if self.pro and self.history_enabled:
            try:
                self.history.add_text(text, source)
            except OSError as exc:
                self.log(f"history write failed: {type(exc).__name__}")

    def record_image(self, data: bytes, info: dict | None, source: str) -> None:
        if self.pro and self.history_enabled and info:
            try:
                self.history.add_image(data, info, source)
            except OSError as exc:
                self.log(f"history write failed: {type(exc).__name__}")

    def set_history_enabled(self, enabled: bool) -> None:
        self.history_enabled = bool(enabled)
        self.config["history_enabled"] = self.history_enabled
        save_config(self.config_path, self.config)

    def restore_entry(self, entry_id: str, source: str = "pc") -> dict:
        """履歴の項目を PC のクリップボードに戻す（再送）。iPhone には SSE で届く。"""
        entry = self.history.get(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        if entry.get("kind") == "image":
            found = self.history.image_bytes(entry_id)
            if found is None:
                raise FileNotFoundError(entry_id)
            data, _ = found
            with self._clip_lock:
                self.clipboard.write_image(data)
                self._own_sequence = self.clipboard.sequence()
            self.state.set("", "image", source, self._store_image(data))
        else:
            text = entry.get("text", "")
            with self._clip_lock:
                self.clipboard.write(text)
                self._own_sequence = self.clipboard.sequence()
            self._store_image(None)
            self.state.set(normalize_newlines(text), "text" if text else "empty", source)
        return self.history.public(entry_id) or {}

    def set_image_settings(self, directory: str | None, subfolder: str | None, template: str | None) -> dict:
        if directory is not None:
            directory = directory.strip()
            if directory:
                self.image_dir = Path(directory).expanduser()
                self.config["image_dir"] = str(self.image_dir)
            else:
                self.image_dir = default_image_dir()
                self.config.pop("image_dir", None)
        if subfolder is not None:
            if subfolder not in IMAGE_SUBFOLDERS:
                raise ValueError("サブフォルダーの指定が不正です")
            self.image_rules["subfolder"] = subfolder
            self.config["image_subfolder"] = subfolder
        if template is not None:
            problem = validate_image_template(template)
            if problem:
                raise ValueError(problem)
            self.image_rules["template"] = template.strip()
            self.config["image_template"] = template.strip()
        save_config(self.config_path, self.config)
        return self.image_settings()

    def image_settings(self) -> dict:
        now = time.localtime()
        template = self.image_rules["template"] if self.pro else DEFAULT_IMAGE_TEMPLATE
        subfolder = self.image_rules["subfolder"] if self.pro else "none"
        example = Path(image_subfolder(subfolder, now)) / render_image_name(template, "IMG_0042.jpg", "jpg", self.pc_name, now)
        return {
            "dir": str(self.image_dir),
            "subfolder": self.image_rules["subfolder"],
            "template": self.image_rules["template"],
            "default_template": DEFAULT_IMAGE_TEMPLATE,
            "tokens": list(IMAGE_TEMPLATE_TOKENS),
            "subfolders": list(IMAGE_SUBFOLDERS),
            "example": str(example),
            "applied": self.pro,
        }

    def hotkey_action(self, action: str) -> None:
        if action == "open_dashboard":
            webbrowser.open(f"http://{HOST}:{self.port}/pc")
        elif action == "restore_phone":
            entry = next((item for item in self.history.entries if item.get("source") == "phone"), None)
            if entry:
                self.restore_entry(entry["id"])
        elif action == "restore_previous":
            entries = list(self.history.entries)
            if not entries:
                return
            index = self._cycle_index + 1
            if index >= len(entries):
                index = 0
            self._cycle_index = index
            self.restore_entry(entries[index]["id"])

    def start_hotkeys(self) -> dict[str, str]:
        if self.pro and self.hotkeys_enabled and HotkeyService.supported:
            errors = self.hotkeys.start(self.hotkey_bindings)
            if errors:
                self.log("hotkeys partially registered: " + ", ".join(errors))
            return errors
        self.hotkeys.stop()
        return {}

    def set_hotkeys(self, enabled: bool | None, bindings: dict | None) -> dict:
        if bindings is not None:
            cleaned: dict[str, str] = {}
            for action in HOTKEY_ACTIONS:
                value = str(bindings.get(action, self.hotkey_bindings[action]) or "").strip()
                parse_hotkey(value)  # 形式が悪ければ ValueError
                cleaned[action] = value
            if len(set(cleaned.values())) != len(cleaned):
                raise ValueError("同じキーを 2 つの操作に割り当てることはできません")
            self.hotkey_bindings = cleaned
            self.config["hotkeys"] = cleaned
        if enabled is not None:
            self.hotkeys_enabled = bool(enabled)
            self.config["hotkeys_enabled"] = self.hotkeys_enabled
        save_config(self.config_path, self.config)
        self.start_hotkeys()
        return self.hotkey_status()

    def hotkey_status(self) -> dict:
        return {
            "supported": HotkeyService.supported,
            "enabled": self.hotkeys_enabled,
            "active": self.hotkeys.active,
            "bindings": dict(self.hotkey_bindings),
            "errors": dict(self.hotkeys.errors),
            "actions": dict(HOTKEY_ACTIONS),
            "defaults": dict(DEFAULT_HOTKEYS),
        }

    def _store_image(self, data: bytes | None) -> dict | None:
        """iPhone に配る画像を保持し、SSE で送るメタ情報を返す。画像でない・大きすぎるときは None（保持も消す）。"""
        info: dict | None = None
        sniffed = sniff_image(data) if data else None
        if data and sniffed and len(data) <= MAX_IMAGE_BYTES:
            kind, ext = sniffed
            width, height = image_dimensions(data) or (0, 0)
            info = {
                "id": hashlib.sha256(data).hexdigest()[:16],
                "size": len(data),
                "width": width,
                "height": height,
                "type": IMAGE_CONTENT_TYPES.get(kind, "application/octet-stream"),
                "ext": ext,
            }
        with self._image_lock:
            self._image = (data, info) if info and data else None
        return info

    def current_image(self) -> tuple[bytes, dict] | None:
        with self._image_lock:
            return self._image

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
                    if sequence > self._own_sequence:  # 自分が書いた内容（画像）を読み戻して再配信しない
                        self._refresh_from_clipboard()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(self.watch_interval)

    def write_clipboard(self, text: str, source: str = "phone", via: str | None = None) -> dict:
        """テキストを PC のクリップボードに入れる。source は phone（iPhone から）か pc（PC 画面から）、via は app / shortcut。"""
        with self._clip_lock:
            self.clipboard.write(text)
            self._own_sequence = self.clipboard.sequence()
        normalized = normalize_newlines(text)
        self._store_image(None)
        self.state.set(normalized, "text" if normalized else "empty", source, via=via)
        self._cycle_index = 0
        self.record_text(normalized, source)
        return self.state.snapshot()

    def receive_image(self, data: bytes, original_name: str, save: bool = True) -> dict:
        """iPhone から届いた画像を PC のクリップボードに入れ、save のときは保存フォルダーにも書く。

        保存ありのときはファイルを優先し、クリップボードへの変換に失敗しても（HEIC が開けない等）warning として返す。
        保存なし（クリップボードだけ）のときは変換に失敗したら例外にする。
        """
        sniffed = sniff_image(data)
        if sniffed is None:
            raise ValueError("画像ファイルとして読めません")
        kind, ext = sniffed
        target: Path | None = None
        if save:
            # Pro の保存ルール（日付フォルダー・ファイル名テンプレート）。無料版は既定のまま
            now = time.localtime()
            folder = self.image_dir
            template = DEFAULT_IMAGE_TEMPLATE
            if self.pro:
                sub = image_subfolder(self.image_rules["subfolder"], now)
                if sub:
                    folder = folder / sub
                template = self.image_rules["template"]
            folder.mkdir(parents=True, exist_ok=True)
            target = unique_path(folder, render_image_name(template, original_name, ext, self.pc_name, now))
            temp = target.with_name(target.name + ".part")
            temp.write_bytes(data)
            os.replace(temp, target)
        warning: str | None = None
        try:
            with self._clip_lock:
                self.clipboard.write_image(data)
                self._own_sequence = self.clipboard.sequence()
        except Exception as exc:  # noqa: BLE001 - 理由だけ返す（内容は出さない）
            self.log(f"clipboard image failed: {type(exc).__name__}")
            warning = str(exc) or type(exc).__name__
            if target is None:
                raise ClipboardError(f"PC のクリップボードに入れられませんでした: {warning}") from exc
        else:
            # 送った iPhone 側にも「PC のクリップボードは今この画像」と伝える（送信元なので新着扱いにはならない）
            info = self._store_image(data)
            self.state.set("", "image", "phone", info)
            self._cycle_index = 0
            self.record_image(data, info, "phone")
        name = target.name if target else f"{safe_stem(original_name)}.{ext}"
        self.last_image = {
            "name": name,
            "path": str(target) if target else None,
            "saved": target is not None,
            "size": len(data),
            "kind": kind,
            "received": time.time(),
            "clipboard": warning is None,
        }
        self.log(
            f"image received: {kind} {len(data)} bytes"
            + (" saved" if target else " clipboard-only")
            + ("" if warning is None else " (clipboard failed)")
        )
        return {
            "ok": True,
            "name": name,
            "path": str(target) if target else None,
            "saved": target is not None,
            "dir": str(self.image_dir),
            "size": len(data),
            "kind": kind,
            "clipboard": warning is None,
            "warning": warning,
        }

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
            self._ts_status = status
        return summary

    def tailnet_suffix(self) -> str | None:
        """自分の MagicDNS 名から tailnet のドメイン（例 tail30626c.ts.net）を得る。CORS の許可判定に使う。"""
        cached_at, cached = self._suffix_cache
        if cached is not None and time.time() - cached_at < 60:
            return cached
        dns = self.tailscale_summary().get("dns_name") or ""
        suffix = dns.split(".", 1)[1].lower() if "." in dns else None
        if suffix:
            self._suffix_cache = (time.time(), suffix)
        return suffix

    def discover_peers(self, force: bool = False) -> dict:
        """同じ tailnet で ClipBridge が動いている他の PC を探す（iPhone 画面の「PC を追加」用）。"""
        with self._peers_lock:
            cached_at, cached = self._peers_cache
            if not force and cached is not None and time.time() - cached_at < 20:
                return cached
        summary = self.tailscale_summary(force=force)
        own = summary.get("dns_name")
        candidates = [peer for peer in tailnet_peers(self._ts_status) if peer["dns"] != own]
        urls = [(peer, public_url(peer["dns"], port)) for peer in candidates for port in HTTPS_PORTS]
        found: list[dict] = []
        if urls:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(urls))) as pool:
                results = list(pool.map(lambda item: (item[0], item[1], self.probe(item[1])), urls))
            seen: set[str] = set()
            for peer, url, health in results:
                if health and peer["dns"] not in seen:
                    seen.add(peer["dns"])
                    found.append({
                        "name": health.get("pc_name") or peer["host"],
                        "host": peer["host"],
                        "url": url,
                        "version": health.get("version"),
                    })
        result = {
            "self": {"name": self.pc_name, "url": summary.get("public_url"), "version": __version__},
            "peers": sorted(found, key=lambda peer: peer["name"].lower()),
            "checked_at": time.time(),
        }
        with self._peers_lock:
            self._peers_cache = (time.time(), result)
        return result

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
            "clip": {
                "kind": snapshot["kind"], "length": snapshot["length"], "updated": snapshot["updated"],
                "hash": snapshot["hash"], "source": snapshot["source"], "image": snapshot["image"],
            },
            "image_dir": str(self.image_dir),
            "last_image": self.last_image,
            "pro": self.pro,
            "license": self.license_status(),
            "history": {"enabled": self.history_enabled, **self.history.counts(), "limit": HISTORY_LIMIT},
            "image_rules": self.image_settings(),
            "hotkeys": self.hotkey_status(),
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
            cors = self._cors_origin()
            if cors:
                self.send_header("Access-Control-Allow-Origin", cors)
                self.send_header("Vary", "Origin")
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

        def _require_pro(self) -> bool:
            if app.pro:
                return True
            self._error(403, "pro_required", "この機能は ClipBridge Pro で使えます。PC のセットアップ画面でライセンスを適用してください")
            return False

        # --- Pro: 履歴 API ----------------------------------------------------
        def _history_get(self, path: str, query: dict[str, str]) -> None:
            if not self._require_token():
                return
            if not self._require_pro():
                return
            app.record_device(self)
            if path == "/api/history":
                try:
                    limit = max(1, min(HISTORY_LIMIT, int(query.get("limit") or HISTORY_LIMIT)))
                except ValueError:
                    limit = HISTORY_LIMIT
                return self._json(200, {"items": app.history.list(query.get("q", ""), limit), **app.history.counts()})
            match = re.fullmatch(r"/api/history/([0-9a-f]{12})(?:/(image))?", path)
            if not match:
                return self._not_found()
            entry_id, sub = match.group(1), match.group(2)
            if sub == "image":
                found = app.history.image_bytes(entry_id)
                if found is None:
                    return self._error(404, "not_found", "この画像は履歴にありません")
                data, entry = found
                return self._send(200, data, entry["type"], {"Content-Disposition": f'inline; filename="clipbridge-{entry_id}.{entry["file"].rsplit(".", 1)[-1]}"'})
            entry = app.history.public(entry_id)
            if entry is None:
                return self._error(404, "not_found", "この項目は履歴にありません")
            return self._json(200, entry)

        def _history_post(self, path: str) -> None:
            if not self._require_token():
                return
            if not self._origin_ok():
                return self._error(403, "origin_mismatch", "Origin が一致しません")
            if not self._require_pro():
                return
            body = self._read_body(4096)
            if body is None:
                return
            try:
                data = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                return self._error(400, "bad_request", "JSON が不正です")
            app.record_device(self)
            if path == "/api/history/clear":
                return self._json(200, {"ok": True, "removed": app.history.clear(keep_pinned=data.get("keep_pinned", True))})
            match = re.fullmatch(r"/api/history/([0-9a-f]{12})/(restore|pin|delete)", path)
            if not match:
                return self._not_found()
            entry_id, action = match.group(1), match.group(2)
            return self._history_action(entry_id, action, data)

        def _history_action(self, entry_id: str, action: str, data: dict) -> None:
            if action == "restore":
                try:
                    entry = app.restore_entry(entry_id)
                except KeyError:
                    return self._error(404, "not_found", "この項目は履歴にありません")
                except (FileNotFoundError, ClipboardError) as exc:
                    return self._error(500, "clipboard", f"クリップボードに戻せませんでした: {exc}")
                app.log("history restored")
                return self._json(200, {"ok": True, "entry": entry})
            if action == "pin":
                entry = app.history.set_pinned(entry_id, bool(data.get("pinned", True)))
                if entry is None:
                    return self._error(404, "not_found", "この項目は履歴にありません")
                return self._json(200, {"ok": True, "entry": entry})
            if action == "delete":
                if not app.history.remove(entry_id):
                    return self._error(404, "not_found", "この項目は履歴にありません")
                return self._json(200, {"ok": True})
            return self._not_found()

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

        def _cors_origin(self) -> str | None:
            """/api/* に対して、同じ tailnet の別 PC で動く ClipBridge 画面（ハブ）からの呼び出しを許す Origin を返す。"""
            origin = self.headers.get("Origin")
            if not origin or not urlsplit(self.path).path.startswith("/api/"):
                return None
            if self._host_matches(origin):
                return origin
            parsed = urlsplit(origin)
            suffix = app.tailnet_suffix()
            host = (parsed.hostname or "").lower()
            if parsed.scheme == "https" and suffix and host.endswith("." + suffix):
                return origin
            return None

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return origin is None or self._cors_origin() is not None

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
            origin = self.headers.get("Origin")
            if origin is not None and not self._host_matches(origin):
                return False  # 管理操作は PC 自身の画面からのみ（他 PC のハブ画面からは不可）
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
                self._error(413, "too_large", f"{max(1, limit // (1024 * 1024))}MB を超えています")
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
                update = app.updater.info
                return self._json(200, {
                    "ok": True, "app": APP_NAME, "version": __version__, "pc_name": app.pc_name, "pro": app.pro,
                    "update_available": bool(update.get("available")),
                    "latest_version": update.get("latest") if update.get("available") else None,
                    "shortcut_url": SHORTCUT_URL,
                })
            if path.startswith("/api/history"):
                return self._history_get(path, query)
            if path.startswith("/pc/api/history"):
                # PC 画面用（トークンなし・ループバック限定）
                if not self._is_admin():
                    return self._not_found()
                if not self._require_pro():
                    return
                match = re.fullmatch(r"/pc/api/history/([0-9a-f]{12})/image", path)
                if match:
                    found = app.history.image_bytes(match.group(1))
                    if found is None:
                        return self._error(404, "not_found", "この画像は履歴にありません")
                    data, entry = found
                    return self._send(200, data, entry["type"])
                if path != "/pc/api/history":
                    return self._not_found()
                try:
                    limit = max(1, min(HISTORY_LIMIT, int(query.get("limit") or HISTORY_LIMIT)))
                except ValueError:
                    limit = HISTORY_LIMIT
                return self._json(200, {"items": app.history.list(query.get("q", ""), limit), **app.history.counts()})
            if path == "/api/peers":
                if not self._require_token():
                    return
                return self._json(200, app.discover_peers(force=query.get("refresh") == "1"))
            if path == "/sw.js":
                return self._serve_sw()
            if path == "/api/clip":
                if not self._require_token():
                    return
                app.record_device(self)
                return self._json(200, app.state.snapshot())
            if path == "/api/image":
                # PC のクリップボードにある画像そのもの（iPhone でプレビュー・保存する）。?id= が今の画像と違えば 404
                if not self._require_token():
                    return
                app.record_device(self)
                current = app.current_image()
                wanted = query.get("id")
                if current is None or (wanted and wanted != current[1]["id"]):
                    return self._error(404, "no_image", "PC のクリップボードに画像がありません（内容が変わりました）")
                data, info = current
                return self._send(
                    200, data, info["type"],
                    {"Content-Disposition": f'inline; filename="clipbridge-{info["id"]}.{info["ext"]}"'},
                )
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
                if path == "/pc/api/clip":
                    # PC 画面の「PC のクリップボード」カード用（本文つき）
                    return self._json(200, app.state.snapshot())
                if path == "/pc/api/clip/image":
                    current = app.current_image()
                    wanted = query.get("id")
                    if current is None or (wanted and wanted != current[1]["id"]):
                        return self._error(404, "no_image", "クリップボードに画像がありません")
                    data, info = current
                    return self._send(200, data, info["type"])
                if path == "/pc/client":
                    return self._redirect(f"/?t={app.token}")
            return self._not_found()

        do_HEAD = do_GET

        def do_OPTIONS(self) -> None:
            """CORS プリフライト。同じ tailnet のハブ画面からの /api/* だけ許す。"""
            origin = self._cors_origin()
            if origin is None:
                return self._error(403, "forbidden", "許可されていない Origin です")
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "X-Token, Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _serve_sw(self) -> None:
            """Service Worker。版をキャッシュ名に埋め込み、更新のたびに古いキャッシュが捨てられるようにする。"""
            path = app.web_dir / "sw.js"
            if not path.is_file():
                return self._not_found()
            script = path.read_text(encoding="utf-8").replace("__VERSION__", __version__)
            self._send(200, script.encode("utf-8"), "text/javascript; charset=utf-8")

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
                cors = self._cors_origin()
                if cors:
                    self.send_header("Access-Control-Allow-Origin", cors)
                    self.send_header("Vary", "Origin")
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
            if path == "/api/image":
                return self._write_image()
            if path.startswith("/api/history/"):
                return self._history_post(path)
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
                # ショートカット（「URL の内容を取得」）からの送信は User-Agent が Shortcuts/… になる
                via = "shortcut" if "Shortcuts" in (self.headers.get("User-Agent") or "") else "app"
                snapshot = app.write_clipboard(text, via=via)
            except Exception as exc:  # noqa: BLE001 - 内容は出さない
                app.log(f"clipboard write failed: {type(exc).__name__}")
                return self._error(500, "clipboard", "PC のクリップボードへ書き込めませんでした")
            app.record_device(self)
            app.log(f"clipboard written: {len(text)} chars")
            self._json(200, {"ok": True, "length": len(text), "hash": snapshot["hash"]})

        def _write_image(self) -> None:
            """iPhone からの画像。本文は画像そのもの、元のファイル名は ?name= で受け取る（形式は中身で判定）。"""
            if not self._require_token():
                return
            if not self._origin_ok():
                return self._error(403, "origin_mismatch", "Origin が一致しません")
            body = self._read_body(MAX_IMAGE_BYTES)
            if body is None:
                return
            if sniff_image(body) is None:
                return self._error(415, "unsupported_media_type", "画像ファイル（JPEG / PNG / GIF / WebP / HEIC）を送ってください")
            query = self._query()
            try:
                result = app.receive_image(body, query.get("name") or "image", save=query.get("save") != "0")
            except ClipboardError as exc:
                return self._error(500, "clipboard", str(exc))
            except (OSError, ValueError) as exc:
                app.log(f"image save failed: {type(exc).__name__}")
                return self._error(500, "image_save_failed", f"画像を保存できませんでした（{type(exc).__name__}）")
            app.record_device(self)
            self._json(200, result)

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
            if action == "clip":
                # PC 画面から文字を入れる（PC のクリップボードに入り、iPhone には「新着」として届く）
                text = data.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._error(400, "bad_request", "送る文字を入力してください")
                if len(text.encode("utf-8")) > MAX_BYTES:
                    return self._error(413, "too_large", "1MB を超えています")
                try:
                    snapshot = app.write_clipboard(text, source="pc")
                except Exception as exc:  # noqa: BLE001 - 内容は出さない
                    app.log(f"clipboard write failed: {type(exc).__name__}")
                    return self._error(500, "clipboard", "PC のクリップボードへ書き込めませんでした")
                app.log(f"clipboard written from dashboard: {len(text)} chars")
                return self._json(200, {"ok": True, "length": len(text), "hash": snapshot["hash"]})
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
            if action == "images/open":
                try:
                    app.image_dir.mkdir(parents=True, exist_ok=True)
                    open_folder(app.image_dir)
                except OSError as exc:
                    return self._error(500, "open_failed", f"フォルダーを開けませんでした: {type(exc).__name__}")
                return self._json(200, {"ok": True, "path": str(app.image_dir)})
            if action == "images/settings":
                wants_pro = data.get("subfolder") is not None or data.get("template") is not None
                if wants_pro and not self._require_pro():
                    return
                try:
                    return self._json(200, app.set_image_settings(data.get("dir"), data.get("subfolder"), data.get("template")))
                except ValueError as exc:
                    return self._error(400, "bad_request", str(exc))
                except OSError as exc:
                    return self._error(500, "save_failed", f"設定を保存できませんでした: {type(exc).__name__}")
            # --- Pro ---
            if action == "license":
                try:
                    info = app.apply_license(str(data.get("key", "")))
                except ValueError as exc:
                    return self._error(400, "invalid_license", str(exc))
                return self._json(200, {"ok": True, "license": info})
            if action == "license/remove":
                app.remove_license()
                return self._json(200, {"ok": True, "license": app.license_status()})
            if action.startswith("history/"):
                if not self._require_pro():
                    return
                sub = action[len("history/"):]
                if sub == "settings":
                    app.set_history_enabled(bool(data.get("enabled")))
                    return self._json(200, {"ok": True, "enabled": app.history_enabled})
                if sub == "clear":
                    return self._json(200, {"ok": True, "removed": app.history.clear(keep_pinned=data.get("keep_pinned", True))})
                entry_id = str(data.get("id", ""))
                if sub in ("restore", "pin", "delete") and HISTORY_ID_RE.fullmatch(entry_id):
                    return self._history_action(entry_id, sub, data)
                return self._not_found()
            if action == "hotkeys":
                if not self._require_pro():
                    return
                try:
                    return self._json(200, app.set_hotkeys(data.get("enabled"), data.get("bindings") if isinstance(data.get("bindings"), dict) else None))
                except ValueError as exc:
                    return self._error(400, "bad_request", str(exc))
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
    app.start_hotkeys()
    app.log(f"{APP_NAME} {__version__} 起動: {dashboard}" + (" (Pro)" if app.pro else ""))
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
        app.hotkeys.stop()
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
