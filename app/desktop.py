"""Free Windows desktop launcher, guided setup, diagnostics and versioned updates."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import webbrowser

import server
from release_info import VERSION, UPDATE_URL, RELEASES_URL

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
HOME = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ClipBridge"
STATE_FILE = HOME / "desktop.json"
MAX_UPDATE_BYTES = 100 * 1024 * 1024


def read_state() -> dict:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(value: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, STATE_FILE)


def version_tuple(value: str) -> tuple:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("更新のバージョン情報が不正です")
    return tuple(int(part) for part in value.split("."))


def validate_manifest(value: dict) -> dict:
    version_tuple(value.get("version"))
    url = urlsplit(value.get("url", ""))
    expected = f"/hiromps/clipbridge/releases/download/v{value['version']}/ClipBridge.exe"
    if url.scheme != "https" or url.hostname != "github.com" or url.path != expected or url.query or url.fragment or url.username:
        raise ValueError("更新ファイルの配布元を確認できません")
    if not re.fullmatch(r"[a-f0-9]{64}", value.get("sha256", "")):
        raise ValueError("更新ファイルの検証情報が不正です")
    if type(value.get("size")) is not int or not 0 < value["size"] <= MAX_UPDATE_BYTES:
        raise ValueError("更新ファイルのサイズが不正です")
    return value


def check_update() -> dict:
    request = urllib.request.Request(UPDATE_URL, headers={"User-Agent": f"ClipBridge/{VERSION}", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(65537)
    except OSError as exc:
        raise RuntimeError("更新情報を取得できません。通信を確認して、あとでもう一度お試しください。現在のアプリはそのまま使えます") from exc
    if len(raw) > 65536:
        raise ValueError("更新情報が大きすぎます")
    return validate_manifest(json.loads(raw))


def download_update(manifest: dict) -> Path:
    manifest = validate_manifest(manifest)
    directory = Path(tempfile.mkdtemp(prefix="ClipBridge-update-"))
    target = directory / "ClipBridge.exe"
    digest = hashlib.sha256()
    size = 0
    try:
        request = urllib.request.Request(manifest["url"], headers={"User-Agent": f"ClipBridge/{VERSION}"})
        with urllib.request.urlopen(request, timeout=30) as response, target.open("wb") as output:
            if urlsplit(response.url).scheme != "https":
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
                raise ValueError("Windowsアプリの形式が不正です")
        return target
    except Exception:
        target.unlink(missing_ok=True)
        directory.rmdir()
        raise


def health(token: str, base: str = "http://127.0.0.1:8787") -> bool:
    try:
        request = urllib.request.Request(base + "/api/health", headers={"X-Token": token})
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response).get("ok") is True
    except (OSError, ValueError):
        return False


def serve_conflict(config: dict) -> bool:
    """Do not replace another app's HTTPS listener, even if only a subpath is used."""
    for key, value in config.get("Web", {}).items():
        if key.endswith(":443"):
            handlers = value.get("Handlers", {})
            if any(path != "/" or handler.get("Proxy") not in ("http://127.0.0.1:8787", "http://localhost:8787")
                   for path, handler in handlers.items()):
                return True
    tcp = config.get("TCP", {}).get("443", {})
    return bool(tcp and not tcp.get("HTTPS"))


def configure_https() -> str:
    executable = server.tailscale_path()
    if not executable:
        raise RuntimeError("Tailscaleを先にインストールしてください")
    result = subprocess.run([executable, "serve", "status", "--json"], capture_output=True, text=True,
                            timeout=8, creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError("公開設定を読み取れません。Tailscaleを接続してから再確認してください")
    config = json.loads(result.stdout or "{}")
    if serve_conflict(config):
        raise RuntimeError("HTTPSの443番は別のサービスで使用中です。既存の設定は変更していません。Tailscaleの公開設定を確認してください")
    # ShellExecute only executes the fixed installed Tailscale CLI and fixed arguments.
    shell = ctypes.windll.shell32.ShellExecuteW
    shell.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    shell.restype = ctypes.c_void_p
    outcome = shell(None, "runas", executable, "serve --bg --https=443 127.0.0.1:8787", None, 0)
    if not outcome or outcome <= 32:
        raise RuntimeError("管理者の許可が得られませんでした。設定は完了していません")
    for _ in range(15):
        time.sleep(1)
        if server.connection_info().get("https_ready"):
            return "HTTPSの公開設定が完了しました。次へ進めます"
    return "設定の完了を確認できません。『HTTPS・DNSの設定を開く』でHTTPSとMagicDNSを有効にしてから、再確認してください"


def install_for_user() -> Path:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("インストールはWindows配布版から利用できます")
    target = HOME / "versions" / VERSION / "ClipBridge.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    if Path(sys.executable).resolve() != target.resolve():
        temporary = target.with_suffix(".new")
        shutil.copy2(sys.executable, temporary)
        os.replace(temporary, target)
    state = read_state()
    previous = state.get("current_executable")
    if previous and previous != str(target) and Path(previous).is_file():
        state["previous_executable"] = previous
    state["current_executable"] = str(target)
    # A stable shortcut is repointed to each installed version. No admin access needed.
    escaped = str(target).replace("'", "''")
    command = (
        "$w=New-Object -ComObject WScript.Shell;"
        "$d=[Environment]::GetFolderPath('Desktop');"
        "$m=Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs';"
        f"foreach($p in @($d,$m)){{$s=$w.CreateShortcut((Join-Path $p 'ClipBridge.lnk'));"
        f"$s.TargetPath='{escaped}';$s.WorkingDirectory='{str(HOME).replace(chr(39), chr(39)*2)}';$s.Save()}}"
    )
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                   check=True, capture_output=True, timeout=20, creationflags=NO_WINDOW)
    save_state(state)
    return target


class DesktopApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.server = None
        self.token = None
        self.busy = False
        self.manifest = None
        self.events = queue.Queue()
        self.state = read_state()
        self.page = int(self.state.get("step", 0)) if self.state.get("step", 0) in range(4) else 0
        self.root.title(f"ClipBridge {VERSION} — 無料のセットアップ")
        self.root.geometry("760x720")
        self.root.minsize(650, 650)
        self.root.configure(bg="#eceef1")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f4f6")
        style.configure("TLabel", background="#f3f4f6", foreground="#1c2b3a", font=("Yu Gothic UI", 11))
        style.configure("Title.TLabel", font=("Yu Gothic UI", 23, "bold"))
        style.configure("TButton", font=("Yu Gothic UI", 10), padding=(13, 9))
        self.frame = ttk.Frame(root, padding=30)
        self.frame.pack(fill="both", expand=True, padx=20, pady=20)
        ttk.Label(self.frame, text="ClipBridge", style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.frame, text="iPhoneとWindowsのクリップボードを、スマートに").pack(anchor="w", pady=(2, 18))
        self.navigation = ttk.Frame(self.frame)
        self.navigation.pack(fill="x")
        for i, name in enumerate(("1 アプリ", "2 Tailscale", "3 iPhone", "診断・更新")):
            ttk.Button(self.navigation, text=name, command=lambda n=i: self.show(n)).pack(side="left", expand=True, fill="x", padx=2)
        viewport = ttk.Frame(self.frame)
        viewport.pack(fill="both", expand=True, pady=(20, 8))
        self.canvas = tk.Canvas(viewport, bg="#f3f4f6", borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(self.canvas)
        content_window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        def layout(event):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            for widget in self.content.winfo_children():
                if isinstance(widget, ttk.Label):
                    widget.configure(wraplength=max(250, event.width - 10))
        self.content.bind("<Configure>", layout)
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(content_window, width=event.width))
        self.root.bind_all("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta / 120), "units"))
        self.status = tk.StringVar(value="")
        ttk.Label(self.frame, textvariable=self.status, wraplength=640).pack(anchor="w", pady=(8, 0))
        self.progress = ttk.Progressbar(self.frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(10, 0))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.show(self.page)
        self.root.after(100, self.drain)
        self.background(self.start_server, lambda text: self.status.set(text))

    def text(self, value, title=False):
        ttk.Label(self.content, text=value, wraplength=630, style="Title.TLabel" if title else "TLabel").pack(anchor="w", pady=(0, 13))

    def button(self, text, command):
        ttk.Button(self.content, text=text, command=command).pack(fill="x", pady=4)

    def show(self, page):
        if getattr(self, "busy", False):
            self.status.set("処理が完了してから画面を切り替えてください")
            return
        self.page = page
        self.state = read_state()
        self.state["step"] = page
        save_state(self.state)
        for item in self.content.winfo_children():
            item.destroy()
        if hasattr(self, "canvas"):
            self.canvas.yview_moveto(0)
        if page == 0:
            self.text("無料で、はじめの準備を", True)
            self.text("Pythonのインストールやコマンド入力は不要です。\n最初はアプリをこのPCへ保存して、次にTailscaleを接続します。\nすべての機能を無料で利用できます。")
            self.button("このPCに保存・デスクトップに追加", self.install)
            self.text("保存先：ユーザーのAppData内。管理者権限は不要です。\n接続トークンはアプリとは別に保存し、更新後も引き継ぎます。")
            self.button("次へ：Tailscaleを接続する", lambda: self.show(1))
        elif page == 1:
            self.text("PCとiPhoneをつなぐ", True)
            self.text("① PCにTailscaleをインストール\n② PCとiPhoneで同じアカウントにログインし、接続をオン\n③ HTTPSの公開設定を行います。必要なときだけWindowsの管理者確認が表示されます。")
            self.button("Windows用Tailscaleを入れる", lambda: webbrowser.open("https://tailscale.com/download/windows"))
            self.button("インストール済み：Tailscaleを開く", self.open_tailscale)
            self.button("HTTPS・DNSの設定を開く", lambda: webbrowser.open("https://login.tailscale.com/admin/dns"))
            self.button("HTTPS公開設定を実行 / 修復", self.repair)
            self.button("接続状態を再確認", self.diagnose)
            self.button("次へ：iPhoneを設定する", lambda: self.show(2))
        elif page == 2:
            self.text("iPhoneを接続する", True)
            self.text("PC側の準備ができたら、接続画面を開きます。\niPhoneにTailscaleを入れ、同じアカウントで接続してください。\n表示されたQRコードをiPhoneのカメラで読み取って接続し、テスト送信まで進めます。")
            self.button("QRコード・接続画面を開く", lambda: self.open_browser(True))
            self.button("iPhone用TailscaleのApp Storeを開く", lambda: webbrowser.open("https://apps.apple.com/jp/app/tailscale/id1470499037"))
            self.text("コントロールセンターから送る場合は、接続画面の\n『接続管理 → ショートカット』で追加と設定ができます。")
            self.button("普段のクリップボード画面を開く", self.open_browser)
            self.button("完了：診断・更新へ", lambda: self.show(3))
        else:
            self.text("診断と更新", True)
            self.text(f"現在のバージョン：{VERSION}\n接続できないときは診断から順番に確認できます。\n更新は内容を確認してから適用します。接続情報は保持されます。")
            self.button("接続を診断する", self.diagnose)
            self.button("ClipBridgeサーバーを起動 / 復旧", lambda: self.background(self.start_server, self.status.set))
            self.button("HTTPS公開設定を修復する", self.repair)
            self.button("クリップボード画面を開く", self.open_browser)
            self.button("更新を確認する（無料）", self.updates)
            self.button("前のインストール版に戻す", self.rollback)
            self.button("ログオン時に自動起動する / 解除する", self.startup)

    def background(self, function, callback):
        if self.busy:
            self.status.set("処理中です。完了してからもう一度お試しください")
            return
        self.busy = True
        self.status.set("確認しています…")
        self.progress.start(12)
        def worker():
            try:
                self.events.put((callback, function(), None))
            except Exception as exc:
                self.events.put((callback, None, exc))
        threading.Thread(target=worker, daemon=True).start()

    def drain(self):
        try:
            callback, value, error = self.events.get_nowait()
            self.busy = False
            self.progress.stop()
            if error:
                self.status.set(str(error))
            else:
                callback(value)
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def start_server(self):
        if self.token is None:
            self.token = server.load_token()
        if health(self.token):
            return "ClipBridgeは起動しています。接続画面を開けます"
        try:
            self.server = server.create_server(token=self.token)
        except OSError:
            raise RuntimeError("8787番ポートを別のアプリまたは旧ClipBridgeが使用中です。旧版を終了してから『サーバーを起動 / 復旧』を押してください。iPhoneの既存設定を保持したい場合は、移行手順をREADMEで確認できます")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return "ClipBridgeを起動しました。ウィンドウを最小化したまま利用できます"

    def install(self):
        def installed(path):
            self.state = read_state()
            self.status.set("保存しました。次回はデスクトップのClipBridgeから開けます")
        self.background(install_for_user, installed)

    def open_tailscale(self):
        executable = server.tailscale_path()
        if not executable:
            self.status.set("Tailscaleが見つかりません。先にインストールしてください")
            return
        gui = Path(executable).with_name("tailscale-ipn.exe")
        if gui.is_file():
            subprocess.Popen([str(gui)], creationflags=NO_WINDOW)
        else:
            self.status.set("タスクバー右下のTailscaleアイコンからログイン・接続してください")

    def diagnose(self):
        def inspect():
            info = server.connection_info()
            if not self.token or not health(self.token):
                return "ClipBridgeサーバーに接続できません。『サーバーを起動 / 復旧』を押してください"
            if info["tailscale"] == "missing":
                return "PCにTailscaleが見つかりません。Windows用をインストールしてください"
            if info["tailscale"] != "connected":
                return "PCのTailscaleが未接続か、状態を取得できません。Tailscaleを開いてログイン・接続してください"
            if not info["dns_name"]:
                return "MagicDNSのアドレスがありません。HTTPS・DNSの設定を確認してください"
            if info["https_ready"] is not True:
                return "HTTPSの公開設定を確認できません。『HTTPS公開設定を実行 / 修復』をお試しください"
            if not health(self.token, "https://" + info["dns_name"]):
                return "PC内では起動していますが、HTTPS接続に失敗しました。TailscaleのDNS・証明書・接続状態を確認してください"
            return "PC側の準備は完了しています。iPhoneで同じTailscaleに接続し、QRコードから開いてください。iPhone側の状態は自動判定できません"
        self.background(inspect, self.status.set)

    def repair(self):
        if not messagebox.askokcancel("HTTPS公開設定", "このPCのClipBridgeをTailscale内だけに公開します。\n必要に応じてWindowsの管理者確認が表示されます。\n別サービスの公開設定がある場合は変更を中止します。"):
            return
        self.background(configure_https, self.status.set)

    def open_browser(self, setup=False):
        def address():
            if not self.token or not health(self.token):
                raise RuntimeError("サーバーを起動 / 復旧してから画面を開いてください")
            name = server.magic_dns_name()
            base = f"https://{name}" if name and health(self.token, f"https://{name}") else "http://127.0.0.1:8787"
            return base + ("/?setup=1#t=" if setup else "/#t=") + self.token
        self.background(address, lambda url: (webbrowser.open(url), self.status.set("接続情報付きでブラウザを開きました")))

    def updates(self):
        def display(manifest):
            if version_tuple(manifest["version"]) <= version_tuple(VERSION):
                self.status.set("最新バージョンです。すべて無料で利用できます")
                return
            if not getattr(sys, "frozen", False):
                self.status.set("ソース版です。配布ページから新しいWindows版を取得してください")
                return
            if not messagebox.askyesno("無料アップデート", f"バージョン {manifest['version']} を適用しますか？\n\n更新ファイルをダウンロードし、SHA-256とサイズを検証します。\n適用時に再起動します。接続情報は引き継ぎ、旧版も保存します。"):
                return
            self.background(lambda: download_update(manifest), self.apply_update)
        self.background(check_update, display)

    def apply_update(self, path):
        subprocess.Popen([str(path), "--install", "--wait-pid", str(os.getpid())], creationflags=NO_WINDOW)
        self.shutdown()

    def rollback(self):
        path = read_state().get("previous_executable")
        if not path or not Path(path).is_file() or not Path(path).resolve().is_relative_to((HOME / "versions").resolve()):
            self.status.set("戻せるインストール済みバージョンはまだありません")
            return
        if messagebox.askyesno("前のバージョンに戻す", "保存してある前のバージョンで再起動しますか？接続情報は保持されます。"):
            subprocess.Popen([path, "--install", "--wait-pid", str(os.getpid())], creationflags=NO_WINDOW)
            self.shutdown()

    def startup(self):
        installed = read_state().get("current_executable")
        if not installed or not Path(installed).is_file():
            self.status.set("先に『このPCに保存・デスクトップに追加』を実行してください")
            return
        folder = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
        target = folder / "ClipBridge.vbs"
        if target.exists():
            if messagebox.askyesno("自動起動", "ClipBridgeのログオン時自動起動を解除しますか？"):
                target.unlink()
                self.status.set("自動起動を解除しました")
        elif messagebox.askyesno("自動起動", "Windowsにログオンしたとき、ClipBridgeを最小化して起動しますか？"):
            # Shortcut is stable across upgrades, unlike a version-specific binary path.
            shortcut = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/ClipBridge.lnk"
            script = 'CreateObject("WScript.Shell").Run """' + str(shortcut).replace('"', '""') + '"" --minimized", 7, False\n'
            target.write_text(script, encoding="utf-16")
            self.status.set("ログオン時に自動起動します")

    def close(self):
        if messagebox.askyesno("ClipBridgeを終了", "終了するとiPhoneから送受信できなくなります。\n終了しますか？\n\n使い続ける場合は『いいえ』を選び、ウィンドウを最小化してください。"):
            self.shutdown()

    def shutdown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.root.destroy()


def wait_for_process(pid):
    if pid <= 0:
        raise ValueError("待機するプロセスが不正です")
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if handle:
        try:
            if kernel.WaitForSingleObject(handle, 30000) != 0:
                raise RuntimeError("旧アプリの終了を確認できません。もう一度起動してください")
        finally:
            kernel.CloseHandle(handle)


def self_test(output):
    """Exercise the frozen bundle without accessing a user's clipboard or settings."""
    import qrcode
    import qrcode.image.svg
    global HOME, STATE_FILE
    original_home, original_state = HOME, STATE_FILE
    original_start = DesktopApp.start_server
    with tempfile.TemporaryDirectory(prefix="ClipBridge-self-test-") as sandbox:
        HOME = Path(sandbox)
        STATE_FILE = HOME / "desktop.json"
        DesktopApp.start_server = lambda self: "self-test"
        window = tk.Tk()
        window.withdraw()
        try:
            gui = DesktopApp(window)
            time.sleep(.15)
            window.update()
            for page in range(4):
                gui.show(page)
                window.update()
        finally:
            window.destroy()
            DesktopApp.start_server = original_start
            HOME, STATE_FILE = original_home, original_state
    qrcode.make("https://example.test/#t=sample", image_factory=qrcode.image.svg.SvgPathImage)
    app = server.create_server(port=0, token="self-test")
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{app.server_address[1]}"
        assert health("self-test", base)
        with urllib.request.urlopen(base + "/") as response:
            assert "ClipBridge" in response.read().decode("utf-8")
        Path(output).write_text(json.dumps({"ok": True, "version": VERSION, "frozen": bool(getattr(sys, "frozen", False))}), encoding="utf-8")
    finally:
        app.shutdown()
        app.server_close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--wait-pid", type=int)
    parser.add_argument("--minimized", action="store_true")
    parser.add_argument("--self-test", metavar="OUTPUT")
    options = parser.parse_args()
    if options.self_test:
        self_test(options.self_test)
        return
    if options.wait_pid:
        wait_for_process(options.wait_pid)
    if options.install:
        target = install_for_user()
        if Path(sys.executable).resolve() != target.resolve():
            subprocess.Popen([str(target)], creationflags=NO_WINDOW)
            return
    root = tk.Tk()
    DesktopApp(root)
    if options.minimized:
        root.iconify()
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if "--self-test" in sys.argv:
            output = sys.argv[sys.argv.index("--self-test") + 1]
            Path(output).write_text(json.dumps({"ok": False, "error": str(exc)}), encoding="utf-8")
            sys.exit(1)
        messagebox.showerror("ClipBridge", str(exc))
