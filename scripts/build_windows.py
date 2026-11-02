"""Build the free Python-independent Windows x64 application and public manifest."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from release_info import VERSION

if sys.platform != "win32":
    raise SystemExit("Run this build on Windows")

subprocess.run([
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "ClipBridge", "--distpath", str(ROOT / "build/windows"),
    "--workpath", str(ROOT / "build/pyinstaller"), "--specpath", str(ROOT / "build"),
    "--add-data", str(ROOT / "app/web") + ";web", "--hidden-import", "qrcode.image.svg",
    "--exclude-module", "PIL", "--exclude-module", "numpy", "--exclude-module", "lxml",
    str(ROOT / "app/desktop.py"),
], check=True, cwd=ROOT)
executable = ROOT / "build/windows/ClipBridge.exe"
download = ROOT / "dist/downloads"
download.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(download / "ClipBridge-Windows.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    archive.write(executable, "ClipBridge/ClipBridge.exe")
    archive.write(ROOT / "app/WINDOWS-README.txt", "ClipBridge/はじめに.txt")
manifest = {
    "version": VERSION,
    "url": f"https://github.com/hiromps/clipbridge/releases/download/v{VERSION}/ClipBridge.exe",
    "size": executable.stat().st_size,
    "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
}
updates = ROOT / "dist/updates"
updates.mkdir(parents=True, exist_ok=True)
(updates / "windows.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
(ROOT / "build/windows/SHA256SUMS.txt").write_text(
    manifest["sha256"] + "  ClipBridge.exe\n" +
    hashlib.sha256((download / "ClipBridge-Windows.zip").read_bytes()).hexdigest() + "  ClipBridge-Windows.zip\n",
    encoding="ascii",
)
print(f"Built free Windows app {VERSION}: {manifest['size']} bytes")
