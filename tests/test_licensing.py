"""発行サーバー（licensing/ と api/）のテスト。Stripe と DB は差し替え、鍵はテスト用に作る。"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from licensing import keys, service as service_module, web  # noqa: E402
from licensing.service import Refused, Service, key_expiry, subscription_state  # noqa: E402
from licensing.store import MemoryStore  # noqa: E402
from licensing.stripe import StripeClient, StripeError, flatten, sign_webhook, verify_webhook  # noqa: E402


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


license_tool = load("license_tool_for_licensing", ROOT / "scripts" / "license_tool.py")
app_server = load("clipbridge_server_for_licensing", ROOT / "app" / "server.py")
PRIVATE = license_tool.keygen(2048)
PUBLIC = {"n": format(PRIVATE["n"], "x"), "e": PRIVATE["e"]}
WEBHOOK_SECRET = "whsec_test"
DAY = 86400
NOW = dt.datetime(2026, 11, 13, 3, 0, tzinfo=dt.timezone.utc).timestamp()
SESSION_ID = "cs_test_a1B2c3D4e5F6g7H8"


class FakeStripe:
    """呼び出しを記録し、決めたオブジェクトを返す。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.prices = {"clipbridge_pro_monthly": "price_month", "clipbridge_pro_yearly": "price_year"}
        self.subscriptions: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}

    def get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(("GET", path, params or {}))
        if path == "/v1/prices":
            price = self.prices.get(params["lookup_keys"][0])
            return {"data": [{"id": price}] if price else []}
        kind, _, object_id = path.rpartition("/")
        found = (self.subscriptions if kind.endswith("subscriptions") else self.sessions).get(object_id)
        if found is None:
            raise StripeError(404, "No such object")
        return found

    def post(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(("POST", path, params or {}))
        if path == "/v1/checkout/sessions":
            return {"id": SESSION_ID, "url": "https://checkout.stripe.com/c/pay/" + SESSION_ID}
        if path == "/v1/billing_portal/sessions":
            return {"url": "https://billing.stripe.com/p/session/test"}
        raise StripeError(404, "unknown")


def subscription(status="trialing", period_end=NOW + 14 * DAY, **extra) -> dict:
    return {
        "id": "sub_1", "status": status, "cancel_at_period_end": False, "cancel_at": None,
        "items": {"data": [{"current_period_end": period_end, "price": {"recurring": {"interval": "month"}}}]},
        **extra,
    }


def checkout_session(**extra) -> dict:
    return {
        "id": SESSION_ID, "mode": "subscription", "status": "complete", "subscription": "sub_1", "customer": "cus_1",
        "metadata": {"product": "clipbridge"}, "customer_details": {"email": "buyer@example.com", "name": "山田 太郎"},
        **extra,
    }


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def env():
    stripe, store, clock = FakeStripe(), MemoryStore(), Clock()
    stripe.subscriptions["sub_1"] = subscription()
    stripe.sessions[SESSION_ID] = checkout_session()
    svc = Service(store=store, stripe=stripe, private_key=PRIVATE, site_url="https://example.test/",
                  webhook_secret=WEBHOOK_SECRET, clock=clock)
    svc.stripe_fake, svc.clock_fake = stripe, clock
    return svc


def event(svc: Service, event_id: str, event_type: str, obj: dict) -> str:
    payload = json.dumps({"id": event_id, "type": event_type, "data": {"object": obj}}).encode()
    return svc.handle_webhook(payload, sign_webhook(payload, WEBHOOK_SECRET, now=svc.clock()))


# --- 鍵 ---------------------------------------------------------------------------

def test_issued_keys_are_accepted_by_the_app_and_readable_after_expiry():
    key = keys.issue(PRIVATE, license_id="CB-202611-ABCD1234", email="a@example.com", name="山田", expires="2999-01-31", now=NOW)
    info = app_server.parse_license(key, PUBLIC)
    assert info["valid"] and info["id"] == "CB-202611-ABCD1234" and info["expires"] == "2999-01-31" and info["name"] == "山田"
    assert keys.read(PRIVATE, key)["email"] == "a@example.com"
    expired = keys.issue(PRIVATE, license_id="CB-X", email="a@example.com", name="", expires="2020-01-01", now=NOW)
    assert app_server.parse_license(expired, PUBLIC)["expired"] and keys.read(PRIVATE, expired)["id"] == "CB-X"
    # CLI（license_tool）で発行した既存のキーも同じ形式で読める
    assert keys.read(PRIVATE, license_tool.issue(PRIVATE, "b@example.com", "", "CB-CLI"))["id"] == "CB-CLI"


def test_keys_from_other_issuers_or_tampered_keys_are_rejected():
    key = keys.issue(PRIVATE, license_id="CB-1", email="a@example.com", name="", expires="2999-01-31")
    other = license_tool.keygen(2048)
    assert keys.read(other, key) is None
    prefix, payload, signature = key.split(".")
    forged = json.loads(keys.b64url_decode(payload))
    forged["id"] = "CB-2"
    assert keys.read(PRIVATE, f"{prefix}.{keys.b64url(json.dumps(forged).encode())}.{signature}") is None
    for bad in (None, 1, "", "not-a-key", "CBP1.a.b", "CBP1." + "x" * 5000, key.replace("CBP1.", "CBP2.")):
        assert keys.read(PRIVATE, bad) is None
    with pytest.raises(ValueError):
        keys.load_private_key("{}")
    with pytest.raises(ValueError):
        keys.load_private_key(json.dumps({"n": "15", "e": "3", "d": "3"}))
    loaded = keys.load_private_key(json.dumps({k: str(v) for k, v in PRIVATE.items()}))
    assert keys.read(loaded, key)["id"] == "CB-1"


# --- Stripe ---------------------------------------------------------------------------

def test_stripe_form_encoding_and_webhook_signature():
    assert flatten({"a": {"b": 1}, "c": [{"d": "x"}], "e": True, "f": None, "g": ["k"]}) == [
        ("a[b]", "1"), ("c[0][d]", "x"), ("e", "true"), ("g[0]", "k"),
    ]
    payload = json.dumps({"id": "evt_1", "type": "x"}).encode()
    header = sign_webhook(payload, "whsec_a", now=1000)
    assert verify_webhook(payload, header, "whsec_a", now=1100)["id"] == "evt_1"
    for bad_payload, bad_header, secret, now in (
        (payload, header, "whsec_b", 1100),          # 別のシークレット
        (payload + b" ", header, "whsec_a", 1100),   # 本文の改変
        (payload, header, "whsec_a", 2000),          # 古すぎる
        (payload, None, "whsec_a", 1100), (payload, "t=abc,v1=00", "whsec_a", 1100), (payload, header, "", 1100),
        (b"[]", sign_webhook(b"[]", "whsec_a", now=1000), "whsec_a", 1000),
    ):
        with pytest.raises(ValueError):
            verify_webhook(bad_payload, bad_header, secret, now=now)


def test_stripe_client_sends_form_requests_and_maps_errors():
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b'{"id": "ok"}'

    def opener(request, timeout=0):
        seen.update(url=request.full_url, method=request.get_method(), data=request.data, auth=request.get_header("Authorization"))
        return Response()

    client = StripeClient("sk_test_x", opener=opener)
    assert client.get("/v1/prices", {"lookup_keys": ["a"]}) == {"id": "ok"}
    assert seen["url"] == "https://api.stripe.com/v1/prices?lookup_keys%5B0%5D=a" and seen["data"] is None
    client.post("/v1/checkout/sessions", {"mode": "subscription"})
    assert seen["method"] == "POST" and seen["data"] == b"mode=subscription" and seen["auth"].startswith("Basic ")

    def failing(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 402, "x", {}, __import__("io").BytesIO(b'{"error": {"message": "card declined"}}'))

    with pytest.raises(StripeError) as error:
        StripeClient("sk_test_x", opener=failing).get("/v1/prices")
    assert error.value.status == 402 and "card declined" in str(error.value)


# --- 期限の決め方 ---------------------------------------------------------------------

def test_key_expiry_rules():
    now = dt.datetime.fromtimestamp(NOW, dt.timezone.utc)
    end = now + dt.timedelta(days=10)
    row = {"status": "active", "current_period_end": end, "cancel_at_period_end": False, "revoked_at": None}
    assert key_expiry(row, now) == (end + dt.timedelta(days=14)).date().isoformat()         # 支払い済み: 猶予 14 日
    assert key_expiry({**row, "status": "trialing"}, now) == (end + dt.timedelta(days=1)).date().isoformat()
    assert key_expiry({**row, "cancel_at_period_end": True}, now) == end.date().isoformat()  # 解約予約: 猶予なし
    assert key_expiry({**row, "status": "past_due"}, now) is not None                        # 再請求中は猶予内
    assert key_expiry({**row, "status": "past_due", "current_period_end": now - dt.timedelta(days=15)}, now) is None
    for dead in ({"status": "canceled"}, {"status": "unpaid"}, {"status": "incomplete"}, {"revoked_at": now}, {"current_period_end": None}):
        assert key_expiry({**row, **dead}, now) is None


def test_subscription_state_reads_both_api_shapes():
    new_shape = subscription_state(subscription(period_end=1000))
    assert new_shape == {"status": "trialing", "period_end": dt.datetime.fromtimestamp(1000, dt.timezone.utc),
                         "cancel_at_period_end": False, "interval": "month"}
    old_shape = subscription_state({"status": "active", "current_period_end": 2000, "cancel_at": 3000, "items": {"data": []}})
    assert old_shape["period_end"].timestamp() == 2000 and old_shape["cancel_at_period_end"] and old_shape["interval"] is None
    assert subscription_state({})["status"] == "incomplete"


# --- 購入からキーの受け取りまで --------------------------------------------------------

def test_checkout_creates_a_trial_subscription_session(env):
    assert env.checkout_url("yearly").startswith("https://checkout.stripe.com/")
    method, path, params = env.stripe_fake.calls[-1]
    assert (method, path) == ("POST", "/v1/checkout/sessions")
    assert params["mode"] == "subscription" and params["line_items"] == [{"price": "price_year", "quantity": 1}]
    assert params["subscription_data"]["trial_period_days"] == 14 and params["metadata"] == {"product": "clipbridge"}
    assert params["success_url"] == "https://example.test/api/license/claim?session_id={CHECKOUT_SESSION_ID}"
    with pytest.raises(Refused) as bad:
        env.checkout_url("lifetime")
    assert bad.value.status == 400
    env.stripe_fake.prices.clear()  # 価格をまだ作っていない
    with pytest.raises(Refused) as missing:
        env.checkout_url("monthly")
    assert missing.value.status == 503


def test_claim_and_webhook_create_one_license_in_either_order(env):
    claimed = env.claim(SESSION_ID)
    info = app_server.parse_license(claimed["key"], PUBLIC)
    assert info["valid"] and info["email"] == "buyer@example.com" and info["name"] == "山田 太郎" and claimed["status"] == "trialing"
    assert claimed["expires"] == dt.datetime.fromtimestamp(NOW + 15 * DAY, dt.timezone.utc).date().isoformat()
    assert event(env, "evt_1", "checkout.session.completed", checkout_session()) == "license"
    assert event(env, "evt_1", "checkout.session.completed", checkout_session()) == "duplicate"
    assert len(env.store.licenses) == 1 and env.claim(SESSION_ID)["id"] == claimed["id"]
    assert claimed["id"].startswith("CB-202611-")


def test_claim_rejects_bad_or_unfinished_sessions(env):
    for bad in ("", "sub_1", "cs_test_../../x", "cs_" + "a" * 300, None):
        with pytest.raises(Refused) as error:
            env.claim(bad)
        assert error.value.status == 400
    with pytest.raises(Refused) as unknown:
        env.claim("cs_test_unknown00000")
    assert unknown.value.status == 404
    for change in ({"status": "open"}, {"mode": "payment"}, {"metadata": {}}, {"subscription": None}):
        env.stripe_fake.sessions[SESSION_ID] = checkout_session(**change)
        with pytest.raises(Refused) as unfinished:
            env.claim(SESSION_ID)
        assert unfinished.value.status == 409
    assert env.store.licenses == {}


def test_webhook_rejects_bad_signatures_and_rolls_back_on_failure(env):
    payload = json.dumps({"id": "evt_9", "type": "checkout.session.completed", "data": {"object": checkout_session()}}).encode()
    with pytest.raises(ValueError):
        env.handle_webhook(payload, sign_webhook(payload, "whsec_wrong", now=NOW))
    del env.stripe_fake.subscriptions["sub_1"]  # Stripe への問い合わせが失敗する
    with pytest.raises(StripeError):
        env.handle_webhook(payload, sign_webhook(payload, WEBHOOK_SECRET, now=NOW))
    assert env.store.events == {} and env.store.licenses == {}  # 記録ごと取り消され、再送で処理し直せる
    env.stripe_fake.subscriptions["sub_1"] = subscription()
    assert env.handle_webhook(payload, sign_webhook(payload, WEBHOOK_SECRET, now=NOW)) == "license"
    assert event(env, "evt_10", "checkout.session.completed", checkout_session(metadata={"product": "other"})) == "ignored"
    assert event(env, "evt_11", "invoice.paid", {"id": "in_1"}) == "ignored"
    assert event(env, "evt_12", "customer.subscription.updated", subscription(id="sub_unknown")) == "ignored"


# --- 更新・解約・返金 ---------------------------------------------------------------------

def test_refresh_follows_the_subscription_lifecycle(env):
    first = env.claim(SESSION_ID)
    body = {"key": first["key"], "install_id": "ab" * 16, "pc_name": "DESKTOP-1", "version": "2.6.0"}
    assert env.refresh(body)["expires"] == first["expires"]
    assert env.store.installs[(first["id"], "ab" * 16)] == {"pc_name": "DESKTOP-1", "version": "2.6.0"}

    # トライアルが終わって課金開始 → 期間 + 猶予 14 日まで延びる
    paid_until = NOW + 44 * DAY
    assert event(env, "evt_2", "customer.subscription.updated", subscription("active", paid_until)) == "updated"
    renewed = env.refresh(body)
    assert renewed["expires"] == dt.datetime.fromtimestamp(paid_until + 14 * DAY, dt.timezone.utc).date().isoformat()
    assert app_server.parse_license(renewed["key"], PUBLIC)["id"] == first["id"]
    # 期限切れの古いキーでも本人確認できる
    env.clock_fake.now = NOW + 40 * DAY
    assert env.refresh({"key": first["key"]})["expires"] == renewed["expires"]

    # 解約を予約 → 期間の終わりまで（猶予なし）
    event(env, "evt_3", "customer.subscription.updated", subscription("active", paid_until, cancel_at_period_end=True))
    assert env.refresh(body)["expires"] == dt.datetime.fromtimestamp(paid_until, dt.timezone.utc).date().isoformat()
    # 契約終了 → 断る（アプリは手元のキーの期限まで使える）
    event(env, "evt_4", "customer.subscription.deleted", subscription("active", paid_until))
    with pytest.raises(Refused) as canceled:
        env.refresh(body)
    assert (canceled.value.status, canceled.value.code) == (403, "canceled")
    # 全額返金 → 取り消し（アプリはキーを外す）
    assert event(env, "evt_5", "charge.refunded", {"refunded": False, "customer": "cus_1"}) == "ignored"
    assert event(env, "evt_6", "charge.refunded", {"refunded": True, "customer": "cus_1"}) == "revoked"
    with pytest.raises(Refused) as revoked:
        env.refresh(body)
    assert (revoked.value.status, revoked.value.code) == (403, "revoked")
    assert event(env, "evt_7", "charge.refunded", {"refunded": True, "customer": "cus_other"}) == "ignored"


def test_refresh_rejects_unknown_and_foreign_keys(env):
    env.claim(SESSION_ID)
    cli_key = license_tool.issue(PRIVATE, "old@example.com", "", "CB-202601-CLI0")  # 無期限の既存キー（DB にない）
    with pytest.raises(Refused) as unknown:
        env.refresh({"key": cli_key})
    assert (unknown.value.status, unknown.value.code) == (403, "unknown")
    foreign = license_tool.issue(license_tool.keygen(2048), "x@example.com", "", next(iter(env.store.licenses)))
    for bad in ({"key": foreign}, {"key": "CBP1.a.b"}, {}, [], None):
        with pytest.raises(Refused) as error:
            env.refresh(bad)
        assert (error.value.status, error.value.code) == (400, "bad_key")
    key = env.claim(SESSION_ID)["key"]
    env.refresh({"key": key, "install_id": "../../etc", "pc_name": "x" * 500})  # 不正な ID は記録しない
    assert env.store.installs == {}


def test_portal_requires_a_known_key(env):
    key = env.claim(SESSION_ID)["key"]
    assert env.portal_url({"key": key}) == "https://billing.stripe.com/p/session/test"
    assert env.stripe_fake.calls[-1][2]["customer"] == "cus_1"
    with pytest.raises(Refused):
        env.portal_url({"key": "CBP1.a.b"})


# --- HTTP（api/ の各ファイルを実際に読み込んで動かす） -------------------------------------

API_FILES = {
    "/api/checkout": "api/checkout.py", "/api/stripe/webhook": "api/stripe/webhook.py",
    "/api/license/claim": "api/license/claim.py", "/api/license/refresh": "api/license/refresh.py",
    "/api/license/portal": "api/license/portal.py",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def call(base: str, path: str, method: str = "GET", data: bytes | None = None, headers: dict | None = None):
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


@pytest.fixture
def http(env, monkeypatch):
    """パスごとに api/ のハンドラへ振り分ける小さなサーバー（Vercel のルーティングの代わり）。"""
    handlers = {path: load("api_" + file.replace("/", "_"), ROOT / file).handler for path, file in API_FILES.items()}
    monkeypatch.setattr(web.Base, "service_factory", staticmethod(lambda **options: env))
    servers = {}
    for path, handler in handlers.items():
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers[path] = httpd

    def request(path: str, **options):
        route = path.split("?")[0]
        return call(f"http://127.0.0.1:{servers[route].server_address[1]}", path, **options)

    yield request
    for httpd in servers.values():
        httpd.shutdown()
        httpd.server_close()


def test_http_purchase_flow(http, env):
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    status, headers, _ = http("/api/checkout", method="POST", data=b"plan=monthly", headers=form)
    assert status == 303 and headers["Location"].startswith("https://checkout.stripe.com/")
    status, headers, body = http("/api/checkout", method="POST", data=b"plan=nope", headers=form)
    assert status == 400 and "text/html" in headers["Content-Type"] and "プラン" in body.decode()
    assert http("/api/checkout")[0] == 405  # GET では決済セッションを作らない（クローラー対策）

    payload = json.dumps({"id": "evt_1", "type": "checkout.session.completed", "data": {"object": checkout_session()}}).encode()
    assert http("/api/stripe/webhook", method="POST", data=payload, headers={"Stripe-Signature": "t=1,v1=00"})[0] == 400
    status, _, body = http("/api/stripe/webhook", method="POST", data=payload,
                           headers={"Stripe-Signature": sign_webhook(payload, WEBHOOK_SECRET, now=NOW)})
    assert status == 200 and json.loads(body) == {"ok": True, "result": "license"}

    status, headers, body = http(f"/api/license/claim?session_id={SESSION_ID}")
    page = body.decode()
    assert status == 200 and headers["Cache-Control"] == "no-store" and headers["X-Robots-Tag"] == "noindex"
    key = page.split('<textarea id="key" readonly spellcheck="false">')[1].split("</textarea>")[0]
    assert app_server.parse_license(key, PUBLIC)["valid"] and "無料トライアル中" in page
    assert http("/api/license/claim?session_id=<script>")[0] == 400 and http("/api/license/claim", method="POST", data=b"")[0] == 405

    json_headers = {"Content-Type": "application/json"}
    status, _, body = http("/api/license/refresh", method="POST", data=json.dumps({"key": key}).encode(), headers=json_headers)
    assert status == 200 and app_server.parse_license(json.loads(body)["key"], PUBLIC)["valid"]
    status, _, body = http("/api/license/portal", method="POST", data=json.dumps({"key": key}).encode(), headers=json_headers)
    assert status == 200 and json.loads(body)["url"].startswith("https://billing.stripe.com/")
    assert http("/api/license/refresh", method="POST", data=b"{", headers=json_headers)[0] == 400
    assert http("/api/license/refresh", method="POST", data=b"x" * (web.MAX_BODY + 1), headers=json_headers)[0] == 400


def test_http_refresh_answers_in_the_shape_the_app_expects(http, env):
    """アプリ側（app/server.py の fetch_license_refresh）を、実際の HTTP 応答に対して動かす。"""
    key = env.claim(SESSION_ID)["key"]
    captured = {}

    def opener(request, timeout=0):
        status, headers, body = http("/api/license/refresh", method="POST", data=request.data, headers=dict(request.header_items()))
        captured["status"] = status
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "x", headers, __import__("io").BytesIO(body))
        return __import__("io").BytesIO(body)

    renewed = app_server.fetch_license_refresh(key, "cd" * 16, "PC", opener=opener)
    assert app_server.parse_license(renewed, PUBLIC)["valid"]
    env.store.revoke_customer("cus_1", dt.datetime.fromtimestamp(NOW, dt.timezone.utc))
    with pytest.raises(app_server.LicenseRefused) as refused:
        app_server.fetch_license_refresh(key, "cd" * 16, "PC", opener=opener)
    assert refused.value.code == "revoked" and captured["status"] == 403


def test_http_reports_missing_configuration_without_details(monkeypatch):
    for name in ("DATABASE_URL", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "LICENSE_PRIVATE_KEY"):
        monkeypatch.delenv(name, raising=False)
    handler = load("api_refresh_unconfigured", ROOT / "api/license/refresh.py").handler
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, _, body = call(f"http://127.0.0.1:{httpd.server_address[1]}", "/api/license/refresh", method="POST",
                               data=b'{"key": "x"}', headers={"Content-Type": "application/json"})
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 503 and json.loads(body)["error"] == "not_ready" and "LICENSE" not in body.decode()


def test_plans_match_the_setup_script():
    setup = load("stripe_setup", ROOT / "scripts" / "stripe_setup.py")
    assert {plan["lookup_key"] for plan in setup.PLANS} == set(service_module.PLANS.values())
    assert {plan["lookup_key"]: (plan["amount"], plan["interval"]) for plan in setup.PLANS} == {
        "clipbridge_pro_monthly": (480, "month"), "clipbridge_pro_yearly": (4800, "year"),
    }
    assert set(setup.WEBHOOK_EVENTS) == {
        "checkout.session.completed", "customer.subscription.updated", "customer.subscription.deleted", "charge.refunded",
    }
