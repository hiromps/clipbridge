#!/usr/bin/env python3
"""ClipBridge Pro の商品・価格・webhook を Stripe に作る（販売者用。何度実行しても同じ結果になる）。

    # PowerShell
    $env:STRIPE_SECRET_KEY = "sk_test_..."   # まずテスト（サンドボックス）のキーで試す
    python scripts/stripe_setup.py            # 署名シークレットを画面に表示する
    python scripts/stripe_setup.py --vercel   # Vercel（Production）の STRIPE_SECRET_KEY と STRIPE_WEBHOOK_SECRET も
                                              # このキーにそろえる（webhook は作り直す。値は表示しない）

作るもの:
    商品    ClipBridge Pro（ID: clipbridge_pro）
    価格    月額 480 円（lookup_key: clipbridge_pro_monthly）/ 年額 4,800 円（clipbridge_pro_yearly）、税込
    webhook <サイト>/api/stripe/webhook（必要なイベントだけ）

無料トライアル（14 日）は価格ではなく、購入時（licensing/service.py の checkout_url）に付ける。
webhook を新しく作ったときだけ、署名シークレット（whsec_...）を 1 回表示する。Vercel の環境変数
STRIPE_WEBHOOK_SECRET に設定すること。シークレットキーはこのスクリプトでは保存も表示もしない。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from licensing.stripe import StripeClient, StripeError  # noqa: E402

PRODUCT_ID = "clipbridge_pro"
PRODUCT_NAME = "ClipBridge Pro"
PLANS = [
    {"lookup_key": "clipbridge_pro_monthly", "amount": 480, "interval": "month", "nickname": "月額"},
    {"lookup_key": "clipbridge_pro_yearly", "amount": 4800, "interval": "year", "nickname": "年額"},
]
WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "charge.refunded",
]
DEFAULT_SITE_URL = "https://clipbridge-ashen.vercel.app"


def ensure_product(stripe: StripeClient) -> str:
    try:
        stripe.get(f"/v1/products/{PRODUCT_ID}")
        return "既にあります"
    except StripeError as exc:
        if exc.status != 404:
            raise
    stripe.post("/v1/products", {
        "id": PRODUCT_ID, "name": PRODUCT_NAME,
        "description": "iPhone と Windows のクリップボード共有 ClipBridge の有料機能（PC 台数無制限・履歴・保存ルール・ホットキー・優先サポート）",
        "metadata": {"product": "clipbridge"},
    })
    return "作成しました"


def ensure_price(stripe: StripeClient, plan: dict) -> str:
    found = stripe.get("/v1/prices", {"lookup_keys": [plan["lookup_key"]], "limit": 1}).get("data") or []
    if found:
        price = found[0]
        same = price.get("unit_amount") == plan["amount"] and price.get("currency") == "jpy" and price.get("active")
        return "既にあります" if same else f"既にありますが内容が違います（{price.get('unit_amount')} {price.get('currency')}）。ダッシュボードで確認してください"
    stripe.post("/v1/prices", {
        "product": PRODUCT_ID, "currency": "jpy", "unit_amount": plan["amount"],
        "recurring": {"interval": plan["interval"]}, "lookup_key": plan["lookup_key"],
        "nickname": plan["nickname"], "tax_behavior": "inclusive", "metadata": {"product": "clipbridge"},
    })
    return "作成しました"


def ensure_webhook(stripe: StripeClient, url: str, recreate: bool = False) -> tuple[str, str | None]:
    """webhook の送信先を用意する。recreate のときは、既にあっても作り直して新しい署名シークレットを受け取る。"""
    replaced = False
    for endpoint in stripe.get("/v1/webhook_endpoints", {"limit": 100}).get("data") or []:
        if endpoint.get("url") == url:
            if recreate:
                stripe.request("DELETE", f"/v1/webhook_endpoints/{endpoint['id']}")
                replaced = True
                continue
            missing = sorted(set(WEBHOOK_EVENTS) - set(endpoint.get("enabled_events") or []))
            if missing and "*" not in (endpoint.get("enabled_events") or []):
                stripe.post(f"/v1/webhook_endpoints/{endpoint['id']}", {"enabled_events": WEBHOOK_EVENTS})
                return "既にあります（イベントを更新しました）", None
            return "既にあります", None
    created = stripe.post("/v1/webhook_endpoints", {
        "url": url, "enabled_events": WEBHOOK_EVENTS, "description": "ClipBridge Pro のライセンス発行",
    })
    return ("作り直しました" if replaced else "作成しました"), created.get("secret")


def save_to_vercel(name: str, value: str) -> bool:
    """値を画面に出さずに、Vercel の環境変数（Production）へ登録する。リンク済みのフォルダーで実行すること。"""
    vercel = shutil.which("vercel")
    if not vercel:
        print("vercel コマンドが見つかりません。", file=sys.stderr)
        return False
    result = subprocess.run(
        [vercel, "env", "add", name, "production", "--sensitive", "--force"],
        input=value, text=True, capture_output=True, cwd=Path(__file__).resolve().parents[1],
    )
    if result.returncode != 0:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        print(f"Vercel への {name} の登録に失敗しました: " + (lines[-1] if lines else "理由不明"), file=sys.stderr)
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClipBridge Pro の Stripe 設定")
    parser.add_argument("--site-url", default=os.environ.get("SITE_URL") or DEFAULT_SITE_URL, help="公開サイトの URL")
    parser.add_argument("--yes", action="store_true", help="確認を省く")
    parser.add_argument("--vercel", action="store_true", help="Vercel（Production）の STRIPE_SECRET_KEY と STRIPE_WEBHOOK_SECRET をこのキーにそろえる（webhook は作り直す）")
    args = parser.parse_args(argv)
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not secret_key.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")):
        print("環境変数 STRIPE_SECRET_KEY に Stripe のシークレットキーを設定してください。", file=sys.stderr)
        return 1
    live = "_live_" in secret_key
    webhook_url = args.site_url.rstrip("/") + "/api/stripe/webhook"
    stripe = StripeClient(secret_key)
    try:
        account = stripe.get("/v1/account")
    except StripeError as exc:
        print(f"Stripe に接続できません: {exc}", file=sys.stderr)
        return 1
    name = (account.get("settings") or {}).get("dashboard", {}).get("display_name") or account.get("id")
    print(f"アカウント: {name}（{account.get('id')}、{'本番' if live else 'テスト'}モード）")
    print(f"作るもの: 商品 {PRODUCT_NAME} / 月額 480 円・年額 4,800 円 / webhook {webhook_url}")
    if args.vercel:
        print("Vercel（Production）: このシークレットキーを STRIPE_SECRET_KEY に、webhook を作り直して署名シークレットを STRIPE_WEBHOOK_SECRET に登録します。")
    if not args.yes and input("このアカウントに作成しますか？ (yes/no): ").strip().lower() != "yes":
        print("中止しました。")
        return 1
    if args.vercel:
        # サイトが使うキーと、ここで商品を作るキーを必ず同じにする（食い違うと購入画面で価格が見つからない）
        if not save_to_vercel("STRIPE_SECRET_KEY", secret_key):
            return 1
        print("Vercel の STRIPE_SECRET_KEY: このキーを登録しました")
    try:
        print(f"商品 {PRODUCT_NAME}: {ensure_product(stripe)}")
        for plan in PLANS:
            print(f"価格 {plan['nickname']}（{plan['lookup_key']}）: {ensure_price(stripe, plan)}")
        status, secret = ensure_webhook(stripe, webhook_url, recreate=args.vercel)
        print(f"webhook: {status}")
        found = [
            plan["nickname"] for plan in PLANS
            if stripe.get("/v1/prices", {"lookup_keys": [plan["lookup_key"]], "active": True, "limit": 1}).get("data")
        ]
        print(f"確認: 購入画面から見える価格 = {'・'.join(found) or 'なし'}")
    except StripeError as exc:
        print(f"Stripe でエラーになりました: {exc}", file=sys.stderr)
        return 1
    if secret and args.vercel and save_to_vercel("STRIPE_WEBHOOK_SECRET", secret):
        print("Vercel の STRIPE_WEBHOOK_SECRET: 新しい署名シークレットを登録しました。再デプロイで反映されます。")
    elif secret:
        print("\n署名シークレット（この 1 回だけ表示します）。Vercel の環境変数 STRIPE_WEBHOOK_SECRET に設定してください:")
        print(secret)
    print("\n残りの手作業: Stripe ダッシュボードの「設定 › Billing › カスタマーポータル」を開いて保存する（解約とカード変更を有効に）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
