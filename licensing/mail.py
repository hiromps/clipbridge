"""購入者へのメール送信（Resend の REST API。標準ライブラリのみ）。"""
from __future__ import annotations

import html
import json
import urllib.error
import urllib.request

API_URL = "https://api.resend.com/emails"


class MailError(Exception):
    pass


class ResendMailer:
    def __init__(self, api_key: str, sender: str, reply_to: str = "", opener=urllib.request.urlopen) -> None:
        self._api_key = api_key
        self.sender = sender
        self.reply_to = reply_to
        self._opener = opener

    def send(self, *, to: str, subject: str, text: str, html_body: str, idempotency_key: str) -> None:
        payload = {"from": self.sender, "to": [to], "subject": subject, "text": text, "html": html_body}
        if self.reply_to:
            payload["reply_to"] = self.reply_to
        request = urllib.request.Request(API_URL, data=json.dumps(payload).encode("utf-8"), method="POST", headers={
            "Authorization": "Bearer " + self._api_key,
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,  # 同じ送信を再試行しても 1 通しか届かない
            "User-Agent": "ClipBridge-Licensing/1",
        })
        try:
            with self._opener(request, timeout=15) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            raise MailError(f"Resend が HTTP {exc.code} を返しました") from exc
        except OSError as exc:
            raise MailError(f"Resend に接続できません（{type(exc).__name__}）") from exc


def license_email(*, key: str, license_id: str, expires: str, trial: bool, claim_url: str, site_url: str) -> dict:
    """購入完了のメール（件名・テキスト・HTML）。"""
    subject = "【ClipBridge Pro】ライセンスキーのお知らせ"
    lead = ("ClipBridge Pro にご登録いただき、ありがとうございます。14 日間の無料トライアルが始まりました。"
            if trial else "ClipBridge Pro にご登録いただき、ありがとうございます。")
    steps = [
        "ClipBridge を使っている PC で、下の「ライセンスを適用するページ」を開く",
        "「この PC の ClipBridge に適用する」を押す（うまく開かないときは、キーをセットアップ画面の「ClipBridge Pro」欄に貼り付けて適用）",
        "2 台目以降の PC は、iPhone の ClipBridge を開くと自動で適用されます",
    ]
    renew = (f"このキーの有効期限は {expires} です。契約が続いているあいだは、アプリが自動で延長します。"
             "期限を過ぎたあとに別の PC へ入力するときも、下のページで最新のキーを使えます。")
    cancel = "解約やカードの変更は、PC のセットアップ画面の「契約を管理（解約・カード変更）」から行えます。"
    text = "\n".join([
        lead, "",
        f"ライセンスキー（ライセンス ID: {license_id}）", key, "",
        "使い方", *[f"{index}. {step}" for index, step in enumerate(steps, 1)], "",
        renew, "",
        "ライセンスを適用するページ（このリンクは他の人に教えないでください）", claim_url, "",
        cancel, "",
        f"特定商取引法に基づく表記: {site_url}/tokushoho",
        "このメールに心当たりがない場合は、お手数ですが破棄してください。",
    ])
    esc = html.escape
    html_body = f"""<div style="font-family: system-ui, -apple-system, 'Segoe UI', 'Hiragino Sans', 'Yu Gothic UI', sans-serif; font-size: 15px; line-height: 1.7; color: #1b2330; max-width: 600px;">
<p>{esc(lead)}</p>
<p><strong>ライセンスキー</strong>（ライセンス ID: {esc(license_id)}）</p>
<pre style="white-space: pre-wrap; word-break: break-all; background: #f4f6fa; border: 1px solid #dfe5ee; border-radius: 8px; padding: 12px; font-size: 12px; line-height: 1.5;">{esc(key)}</pre>
<p><strong>使い方</strong></p>
<ol>{''.join(f'<li>{esc(step)}</li>' for step in steps)}</ol>
<p>{esc(renew)}</p>
<p><a href="{esc(claim_url, quote=True)}">ライセンスを適用するページを開く</a><br><span style="color: #5b6677; font-size: 13px;">このリンクは他の人に教えないでください。</span></p>
<p>{esc(cancel)}</p>
<p style="color: #5b6677; font-size: 13px;"><a href="{esc(site_url, quote=True)}/tokushoho">特定商取引法に基づく表記</a><br>このメールに心当たりがない場合は、お手数ですが破棄してください。</p>
</div>"""
    return {"subject": subject, "text": text, "html_body": html_body}
