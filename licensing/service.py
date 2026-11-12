"""購入・webhook・キーの受け取り・更新・契約管理のロジック（HTTP と保存先から独立）。"""
from __future__ import annotations

import datetime as dt
import re
import time

from . import keys
from .stripe import StripeError, verify_webhook

PRODUCT = "clipbridge"
# Stripe の価格は lookup_key で引く（scripts/stripe_setup.py が作る）
PLANS = {"monthly": "clipbridge_pro_monthly", "yearly": "clipbridge_pro_yearly"}
TRIAL_DAYS = 14
GRACE_DAYS = 14        # 支払い済みの期間が終わってからキーが切れるまでの猶予（更新の遅れ・再請求を吸収する）
TRIAL_GRACE_DAYS = 1   # トライアル中は猶予をほぼ付けない（解約後に使える日数を増やさない）
LIVE_STATUSES = ("active", "trialing", "past_due")
SESSION_ID_RE = re.compile(r"cs_[A-Za-z0-9_]{10,200}")


class Refused(Exception):
    """利用者に理由を返して断る。status は HTTP ステータス、code はアプリが見る理由。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _timestamp(value) -> dt.datetime | None:
    return dt.datetime.fromtimestamp(int(value), dt.timezone.utc) if isinstance(value, (int, float)) and value else None


def subscription_state(subscription: dict) -> dict:
    """Stripe の subscription から、保存する項目を取り出す（API の版による置き場所の違いを吸収する）。"""
    items = (subscription.get("items") or {}).get("data") or []
    first = items[0] if items and isinstance(items[0], dict) else {}
    period_end = subscription.get("current_period_end") or first.get("current_period_end")
    recurring = ((first.get("price") or {}).get("recurring") or {}) if isinstance(first.get("price"), dict) else {}
    return {
        "status": str(subscription.get("status") or "incomplete"),
        "period_end": _timestamp(period_end),
        "cancel_at_period_end": bool(subscription.get("cancel_at_period_end") or subscription.get("cancel_at")),
        "interval": recurring.get("interval") if isinstance(recurring.get("interval"), str) else None,
    }


def key_expiry(license_row: dict, now: dt.datetime) -> str | None:
    """いま発行するキーの有効期限（YYYY-MM-DD）。発行できない状態なら None。"""
    if license_row.get("revoked_at") or license_row.get("status") not in LIVE_STATUSES:
        return None
    period_end = license_row.get("current_period_end")
    if period_end is None:
        return None
    if license_row.get("cancel_at_period_end"):
        grace = 0
    elif license_row["status"] == "trialing":
        grace = TRIAL_GRACE_DAYS
    else:
        grace = GRACE_DAYS
    expires = (period_end + dt.timedelta(days=grace)).date()
    return expires.isoformat() if expires >= now.date() else None


class Service:
    def __init__(self, *, store, stripe, private_key: dict, site_url: str, webhook_secret: str = "", clock=time.time) -> None:
        self.store = store
        self.stripe = stripe
        self.private_key = private_key
        self.site_url = site_url.rstrip("/")
        self.webhook_secret = webhook_secret
        self.clock = clock

    def _now(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc)

    # --- 購入 -------------------------------------------------------------------
    def checkout_url(self, plan: str) -> str:
        lookup_key = PLANS.get(plan)
        if not lookup_key:
            raise Refused(400, "bad_plan", "プランを選び直してください")
        prices = self.stripe.get("/v1/prices", {"lookup_keys": [lookup_key], "active": True, "limit": 1}).get("data") or []
        if not prices:
            raise Refused(503, "not_ready", "ただいま購入を受け付けていません")
        session = self.stripe.post("/v1/checkout/sessions", {
            "mode": "subscription",
            "line_items": [{"price": prices[0]["id"], "quantity": 1}],
            "subscription_data": {"trial_period_days": TRIAL_DAYS, "metadata": {"product": PRODUCT}},
            "metadata": {"product": PRODUCT},
            "locale": "ja",
            "success_url": self.site_url + "/api/license/claim?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": self.site_url + "/#pricing",
        })
        return session["url"]

    # --- 契約 → ライセンス --------------------------------------------------------
    def _license_for_session(self, session: dict) -> dict | None:
        """完了した Checkout セッションに対応するライセンスを作る（webhook と受け取りページの両方から呼ばれる）。"""
        if session.get("mode") != "subscription" or (session.get("metadata") or {}).get("product") != PRODUCT:
            return None
        subscription_id, customer_id = session.get("subscription"), session.get("customer")
        if session.get("status") != "complete" or not isinstance(subscription_id, str) or not isinstance(customer_id, str):
            return None
        subscription = self.stripe.get(f"/v1/subscriptions/{subscription_id}")
        state = subscription_state(subscription)
        details = session.get("customer_details") or {}
        return self.store.upsert_subscription(
            subscription_id=subscription_id, customer_id=customer_id,
            email=str(details.get("email") or session.get("customer_email") or ""),
            name=str(details.get("name") or ""),
            status=state["status"], period_end=state["period_end"],
            cancel_at_period_end=state["cancel_at_period_end"], interval=state["interval"], now=self._now(),
        )

    def _issue(self, license_row: dict) -> dict:
        expires = key_expiry(license_row, self._now())
        if expires is None:
            if license_row.get("revoked_at"):
                raise Refused(403, "revoked", "このライセンスは取り消されました")
            raise Refused(403, "canceled", "契約が有効ではありません。お支払い状況をご確認ください")
        key = keys.issue(
            self.private_key, license_id=license_row["id"], email=license_row["email"], name=license_row["name"],
            expires=expires, now=self.clock(),
        )
        return {"key": key, "expires": expires, "id": license_row["id"]}

    # --- webhook ----------------------------------------------------------------
    def handle_webhook(self, payload: bytes, signature: str | None) -> str:
        event = verify_webhook(payload, signature, self.webhook_secret, now=self.clock())
        obj = ((event.get("data") or {}).get("object")) or {}
        with self.store.transaction():  # 途中で失敗したらイベントの記録ごと取り消し、Stripe の再送に任せる
            if not self.store.record_event(event["id"], event["type"]):
                return "duplicate"
            if event["type"] == "checkout.session.completed":
                return "license" if self._license_for_session(obj) else "ignored"
            if event["type"] in ("customer.subscription.updated", "customer.subscription.deleted"):
                state = subscription_state(obj)
                if event["type"] == "customer.subscription.deleted":
                    state["status"] = "canceled"
                updated = self.store.update_subscription(
                    subscription_id=str(obj.get("id") or ""), status=state["status"], period_end=state["period_end"],
                    cancel_at_period_end=state["cancel_at_period_end"], interval=state["interval"],
                )
                return "updated" if updated else "ignored"
            if event["type"] == "charge.refunded":
                # 全額返金したら、その顧客のライセンスを取り消す（アプリは次の確認でキーを外す）
                if obj.get("refunded") is True and isinstance(obj.get("customer"), str):
                    return "revoked" if self.store.revoke_customer(obj["customer"], self._now()) else "ignored"
                return "ignored"
        return "ignored"

    # --- キーの受け取り（購入直後のページ） -------------------------------------------
    def claim(self, session_id: str) -> dict:
        if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
            raise Refused(400, "bad_session", "購入の確認用リンクが正しくありません")
        try:
            session = self.stripe.get(f"/v1/checkout/sessions/{session_id}")
        except StripeError as exc:
            if exc.status == 404:
                raise Refused(404, "bad_session", "購入を確認できませんでした") from exc
            raise
        with self.store.transaction():
            license_row = self._license_for_session(session)
        if not license_row:
            raise Refused(409, "not_complete", "お支払いの手続きがまだ完了していません")
        return {**self._issue(license_row), "email": license_row["email"], "status": license_row["status"]}

    # --- 更新（アプリが 1 日 1 回まで呼ぶ） ---------------------------------------------
    def _license_from_key(self, data: object) -> dict:
        payload = keys.read(self.private_key, data.get("key") if isinstance(data, dict) else None)
        if payload is None:
            raise Refused(400, "bad_key", "ライセンスキーを確認できません")
        license_row = self.store.get_license(payload["id"])
        if license_row is None:
            raise Refused(403, "unknown", "このライセンスは自動更新の対象ではありません")
        return license_row

    def refresh(self, data: object) -> dict:
        license_row = self._license_from_key(data)
        issued = self._issue(license_row)
        install_id = data.get("install_id")
        if isinstance(install_id, str) and re.fullmatch(r"[0-9a-f]{16,64}", install_id):
            self.store.touch_install(
                license_row["id"], install_id,
                str(data.get("pc_name") or "")[:100], str(data.get("version") or "")[:32],
            )
        return {"key": issued["key"], "expires": issued["expires"]}

    # --- 契約の管理（解約・カード変更） ---------------------------------------------------
    def portal_url(self, data: object) -> str:
        license_row = self._license_from_key(data)
        session = self.stripe.post("/v1/billing_portal/sessions", {
            "customer": license_row["stripe_customer_id"], "return_url": self.site_url + "/", "locale": "ja",
        })
        return session["url"]
