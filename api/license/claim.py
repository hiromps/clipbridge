"""GET /api/license/claim?session_id=...（購入直後のページ。ライセンスキーを表示する）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリのルート（licensing/ がある場所）

from licensing.web import ClaimHandler  # noqa: E402


class handler(ClaimHandler):
    pass
