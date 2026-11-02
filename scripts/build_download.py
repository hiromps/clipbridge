"""Build the Windows distribution from an explicit public-file allowlist."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "server.py", "ClipBridge.bat", "launch_clipbridge.ps1", "setup_tailscale.ps1",
    "install_startup.ps1", "make_icon.py", "README.md", "requirements.txt",
    "web/index.html", "web/manifest.webmanifest", "web/icon-180.png",
]
target = ROOT / "dist/downloads/ClipBridge-Windows.zip"
target.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
    for name in FILES:
        source = ROOT / "app" / name
        archive.write(source, Path("ClipBridge") / name)
print(f"Built {target.name} from {len(FILES)} public application files")
