"""POST /api/license/portal（キーを見せると、解約・カード変更ページの URL を返す）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリのルート（licensing/ がある場所）

from licensing.web import PortalHandler  # noqa: E402


class handler(PortalHandler):
    pass
