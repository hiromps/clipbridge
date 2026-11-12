"""POST /api/license/refresh（アプリからの更新確認。契約が続いていれば期限を延ばしたキーを返す）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリのルート（licensing/ がある場所）

from licensing.web import RefreshHandler  # noqa: E402


class handler(RefreshHandler):
    pass
