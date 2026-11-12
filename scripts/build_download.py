"""ソース配布 ZIP（Python で動かす人向け）を公開ファイルの許可リストから作る。

    python scripts/build_download.py  → build/source/ClipBridge-Source.zip

設定ファイル（config.json）やビルド成果物は含めない。
"""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "server.py", "ClipBridge.bat", "README.md", "WINDOWS-README.txt", "requirements-dev.txt",
    "web/index.html", "web/app.js", "web/pc.html", "web/pc.js", "web/style.css",
    "web/sw.js", "web/icon-180.png", "web/icon-512.png", "web/vendor/qrcode.js", "web/vendor/LICENSE-qrcode-generator.txt",
    "tests/test_server.py", "tools/make_icons.py",
]
# 説明用の図解（web/img/）は増減するので、フォルダーの中身をそのまま入れる
FILES += sorted(path.relative_to(ROOT / "app").as_posix() for path in (ROOT / "app" / "web" / "img").glob("*.webp"))
target = ROOT / "build" / "source" / "ClipBridge-Source.zip"
target.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
    for name in FILES:
        archive.write(ROOT / "app" / name, Path("ClipBridge") / name)
print(f"Built {target} from {len(FILES)} files")
