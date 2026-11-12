"""POST /api/stripe/webhook（Stripe からの通知を受けて契約の状態を保存する）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリのルート（licensing/ がある場所）

from licensing.web import WebhookHandler  # noqa: E402


class handler(WebhookHandler):
    pass
