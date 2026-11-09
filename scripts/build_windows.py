"""Windows 用の配布物をビルドする（PyInstaller の単一 exe、配布 ZIP、更新マニフェスト）。

    python -m pip install pyinstaller
    python scripts/build_windows.py

出力は build/windows/ に置く:
  ClipBridge.exe            本体（Python 不要、ウィンドウなし。ダブルクリックで PC セットアップ画面が開く）
  ClipBridge-Windows.zip    配布 ZIP（exe と はじめに.txt）
  windows.json              更新マニフェスト（version / url / size / sha256）。アプリの更新確認が読む
  SHA256SUMS.txt            検証用ハッシュ

バージョンは app/server.py の __version__ から読む。利用者の設定やトークンは含まれない。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
OUT = ROOT / "build" / "windows"
REPO = "hiromps/clipbridge"


def read_version() -> str:
    source = (APP / "server.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "(\d+\.\d+\.\d+)"', source, re.MULTILINE)
    if not match:
        raise SystemExit("app/server.py に __version__ = \"x.y.z\" が見つかりません")
    return match.group(1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Windows 上でビルドしてください")
    version = read_version()
    OUT.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "ClipBridge",
        "--distpath", str(OUT),
        "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build"),
        "--add-data", f"{APP / 'web'};web",
        "--exclude-module", "tkinter",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
    ]
    icon = APP / "ClipBridge.ico"
    if icon.exists():
        command += ["--icon", str(icon)]
    command.append(str(APP / "server.py"))
    subprocess.run(command, check=True, cwd=ROOT)

    exe = OUT / "ClipBridge.exe"
    archive_path = OUT / "ClipBridge-Windows.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(exe, "ClipBridge/ClipBridge.exe")
        archive.write(APP / "WINDOWS-README.txt", "ClipBridge/はじめに.txt")

    manifest = {
        "version": version,
        "url": f"https://github.com/{REPO}/releases/download/v{version}/ClipBridge.exe",
        "size": exe.stat().st_size,
        "sha256": sha256(exe),
    }
    (OUT / "windows.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (OUT / "SHA256SUMS.txt").write_text(
        f"{manifest['sha256']}  ClipBridge.exe\n{sha256(archive_path)}  ClipBridge-Windows.zip\n",
        encoding="ascii",
    )
    print(f"ClipBridge {version}: {exe} ({manifest['size']} bytes)")
    print(f"  {archive_path}\n  {OUT / 'windows.json'}\n  {OUT / 'SHA256SUMS.txt'}")


if __name__ == "__main__":
    main()
