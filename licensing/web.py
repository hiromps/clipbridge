"""HTTP ハンドラ。api/ 配下の各ファイルが、ここのクラスを 1 つずつ Vercel Function として公開する。

環境変数:
    DATABASE_URL            Neon の接続文字列（プール接続）
    STRIPE_SECRET_KEY       Stripe のシークレットキー
    STRIPE_WEBHOOK_SECRET   webhook の署名シークレット（whsec_...）
    LICENSE_PRIVATE_KEY     license_tool.py keygen が作った秘密鍵の JSON
    SITE_URL                公開サイトの URL（省略時は既定の本番 URL）
"""
from __future__ import annotations

import html
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from . import keys
from .service import Refused, Service
from .store import PostgresStore
from .stripe import StripeClient, StripeError

DEFAULT_SITE_URL = "https://clipbridge-ashen.vercel.app"
MAX_BODY = 64 * 1024


class NotConfigured(Exception):
    pass


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise NotConfigured(name)
    return value


class _NoStore:
    """DB を使わないエンドポイント用。誤って使ったら気づけるようにする。"""

    def __getattr__(self, name):
        raise NotConfigured("DATABASE_URL")

    def close(self) -> None:
        pass


def build_service(*, database: bool, webhook: bool = False) -> Service:
    """リクエストごとに組み立てる（接続は呼び出し側が service.store.close() で閉じる）。"""
    private_key = keys.load_private_key(_env("LICENSE_PRIVATE_KEY"))
    stripe = StripeClient(_env("STRIPE_SECRET_KEY"))
    webhook_secret = _env("STRIPE_WEBHOOK_SECRET") if webhook else ""
    store = PostgresStore.connect(_env("DATABASE_URL")) if database else _NoStore()
    return Service(
        store=store, stripe=stripe, private_key=private_key, webhook_secret=webhook_secret,
        site_url=os.environ.get("SITE_URL", "").strip() or DEFAULT_SITE_URL,
    )


class Base(BaseHTTPRequestHandler):
    """共通の入出力。サブクラスは handle_get / handle_post を実装し、(status, 種類, 内容) を返す。"""

    server_version = "ClipBridge-Licensing"
    sys_version = ""
    database = True
    webhook = False
    service_factory = staticmethod(build_service)  # テストで差し替える

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 本文やクエリはログに出さない
        pass

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _page(self, status: int, title: str, body_html: str) -> None:
        self._send(status, render_page(title, body_html).encode("utf-8"), "text/html; charset=utf-8")

    def _redirect(self, location: str) -> None:
        self._send(303, b"", "text/plain; charset=utf-8", {"Location": location})

    def _body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if length < 0:
            return None
        if length > MAX_BODY:
            # 送信側が応答を受け取れるよう、上限ぶんだけ読み捨ててから閉じる
            self.rfile.read(MAX_BODY + 1)
            self.close_connection = True
            return None
        return self.rfile.read(length)

    def _query(self) -> dict[str, str]:
        return {name: values[0] for name, values in parse_qs(urlsplit(self.path).query).items() if values}

    wants_html = False  # エラーを HTML で返すか（ブラウザーで開くエンドポイント）

    def _fail(self, status: int, code: str, message: str) -> None:
        if self.wants_html:
            return self._page(status, "ClipBridge Pro", f"<h1>{html.escape(message)}</h1><p><a href=\"/\">ClipBridge のサイトへ戻る</a></p>")
        self._json(status, {"error": code, "message": message})

    def _run(self, action) -> None:
        service = None
        try:
            service = self.service_factory(database=self.database, webhook=self.webhook)
            action(service)
        except Refused as exc:
            self._fail(exc.status, exc.code, str(exc))
        except NotConfigured as exc:
            print(f"licensing: {exc} is not set", file=sys.stderr)
            self._fail(503, "not_ready", "ただいま準備中です。しばらくしてからお試しください")
        except StripeError as exc:
            print(f"licensing: stripe error {exc.status}", file=sys.stderr)
            self._fail(502, "upstream", "決済サービスに接続できませんでした。しばらくしてからお試しください")
        except Exception as exc:  # noqa: BLE001 - 詳細は返さない
            print(f"licensing: {type(exc).__name__}", file=sys.stderr)
            self._fail(500, "server_error", "サーバーでエラーが起きました。しばらくしてからお試しください")
        finally:
            if service is not None:
                try:
                    service.store.close()
                except Exception:  # noqa: BLE001
                    pass

    def _not_allowed(self) -> None:
        self._fail(405, "method_not_allowed", "使用できないメソッドです")

    do_GET = do_POST = do_HEAD = _not_allowed


class CheckoutHandler(Base):
    """POST /api/checkout（フォームの plan=monthly|yearly）→ Stripe の決済ページへ。"""

    database = False
    wants_html = True

    def do_POST(self) -> None:
        body = self._body()
        if body is None:
            return self._fail(413, "too_large", "リクエストが大きすぎます")
        form = parse_qs(body.decode("utf-8", "replace"))
        plan = (form.get("plan") or [""])[0]
        self._run(lambda service: self._redirect(service.checkout_url(plan)))


class WebhookHandler(Base):
    """POST /api/stripe/webhook（Stripe からの通知）。"""

    webhook = True

    def do_POST(self) -> None:
        body = self._body()
        if body is None:
            return self._fail(413, "too_large", "リクエストが大きすぎます")
        signature = self.headers.get("Stripe-Signature")

        def action(service: Service) -> None:
            try:
                result = service.handle_webhook(body, signature)
            except ValueError:
                return self._fail(400, "bad_signature", "署名を確認できません")
            self._json(200, {"ok": True, "result": result})

        self._run(action)


class ClaimHandler(Base):
    """GET /api/license/claim?session_id=...（購入直後に開くページ。ライセンスキーを表示する）。"""

    wants_html = True

    def do_GET(self) -> None:
        session_id = self._query().get("session_id", "")
        self._run(lambda service: self._page(200, "ClipBridge Pro のライセンスキー", render_claim(service.claim(session_id))))


class RefreshHandler(Base):
    """POST /api/license/refresh（アプリからの更新確認）。"""

    def do_POST(self) -> None:
        data = self._read_json()
        if data is not None:
            self._run(lambda service: self._json(200, service.refresh(data)))

    def _read_json(self) -> dict | None:
        body = self._body()
        try:
            data = json.loads(body.decode("utf-8")) if body is not None else None
        except (ValueError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            self._fail(400, "bad_request", "JSON が不正です")
            return None
        return data


class PortalHandler(RefreshHandler):
    """POST /api/license/portal（キーを見せると、解約・カード変更ページの URL を返す）。"""

    def do_POST(self) -> None:
        data = self._read_json()
        if data is not None:
            self._run(lambda service: self._json(200, {"url": service.portal_url(data)}))


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>__TITLE__</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; font: 16px/1.7 system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", sans-serif; background: #f4f6fa; color: #1b2330; }
  main { max-width: 640px; margin: 0 auto; padding: 40px 20px 64px; }
  h1 { font-size: 1.5rem; line-height: 1.4; margin: 0 0 12px; }
  .card { background: #fff; border: 1px solid #dfe5ee; border-radius: 14px; padding: 20px; margin: 20px 0; }
  textarea { width: 100%; box-sizing: border-box; min-height: 150px; padding: 12px; border: 1px solid #c9d2df; border-radius: 10px; font: 13px/1.5 ui-monospace, Consolas, monospace; word-break: break-all; resize: vertical; background: #f8fafc; color: inherit; }
  button, .btn { display: inline-block; margin-top: 12px; padding: 10px 18px; border: 0; border-radius: 10px; background: #2563eb; color: #fff; font: inherit; font-weight: 600; cursor: pointer; text-decoration: none; }
  ol { padding-left: 1.3em; margin: 8px 0 0; }
  .note { color: #5b6677; font-size: .92rem; }
  a { color: #2563eb; }
  @media (prefers-color-scheme: dark) {
    body { background: #0f1420; color: #e6ebf3; }
    .card { background: #171e2c; border-color: #2a3447; }
    textarea { background: #0f1420; border-color: #2a3447; }
    .note { color: #9aa6b8; }
  }
</style>
</head>
<body><main>__BODY__</main></body>
</html>
"""


def render_page(title: str, body_html: str) -> str:
    return PAGE.replace("__TITLE__", html.escape(title)).replace("__BODY__", body_html)


def render_claim(result: dict) -> str:
    key, expires, license_id = (html.escape(str(result[name])) for name in ("key", "expires", "id"))
    trial = "無料トライアル中です。" if result.get("status") == "trialing" else ""
    return f"""
<h1>ご登録ありがとうございます</h1>
<p>{trial}下のライセンスキーを PC の ClipBridge に貼り付けると、Pro の機能が使えるようになります。</p>
<div class="card">
  <label for="key"><strong>ライセンスキー</strong>（ライセンス ID: {license_id}）</label>
  <textarea id="key" readonly spellcheck="false">{key}</textarea>
  <button type="button" id="copy">キーをコピー</button>
  <p class="note" id="copied" aria-live="polite"></p>
</div>
<div class="card">
  <strong>使い方</strong>
  <ol>
    <li>PC で ClipBridge のセットアップ画面を開く</li>
    <li>「ClipBridge Pro」の欄にキーを貼り付けて「ライセンスを適用」を押す</li>
    <li>2 台目以降の PC は、iPhone の ClipBridge を開くと自動で適用されます</li>
  </ol>
</div>
<p class="note">このキーの有効期限は {expires} です。契約が続いているあいだは、アプリが自動で延長します。
このページはブックマークしておくと、あとからキーを確認できます。解約やカードの変更は、PC のセットアップ画面の「契約を管理」から行えます。</p>
<script>
document.getElementById('copy').addEventListener('click', async () => {{
  const field = document.getElementById('key');
  try {{ await navigator.clipboard.writeText(field.value); }} catch (e) {{ field.select(); document.execCommand('copy'); }}
  document.getElementById('copied').textContent = 'コピーしました。';
}});
</script>
"""
