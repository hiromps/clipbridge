"""POST /api/checkout（フォームの plan=monthly|yearly）→ Stripe の決済ページへ移動する。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # リポジトリのルート（licensing/ がある場所）

from licensing.web import CheckoutHandler  # noqa: E402


class handler(CheckoutHandler):
    pass
