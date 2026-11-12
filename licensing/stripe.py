"""Stripe の REST 呼び出しと webhook の署名検証（標準ライブラリのみ）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.stripe.com"
WEBHOOK_TOLERANCE = 300  # 署名の時刻のずれをここまで許す（秒）


class StripeError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def flatten(params: dict, prefix: str = "") -> list[tuple[str, str]]:
    """{"a": {"b": 1}, "c": [{"d": 2}]} → a[b]=1, c[0][d]=2（Stripe のフォーム形式）。"""
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            pairs.extend(flatten(value, name))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    pairs.extend(flatten(item, f"{name}[{index}]"))
                else:
                    pairs.append((f"{name}[{index}]", str(item)))
        elif isinstance(value, bool):
            pairs.append((name, "true" if value else "false"))
        elif value is not None:
            pairs.append((name, str(value)))
    return pairs


class StripeClient:
    def __init__(self, secret_key: str, opener=urllib.request.urlopen) -> None:
        self._auth = "Basic " + base64.b64encode((secret_key + ":").encode("ascii")).decode("ascii")
        self._opener = opener

    def request(self, method: str, path: str, params: dict | None = None) -> dict:
        encoded = urllib.parse.urlencode(flatten(params or {}))
        url = API_BASE + path
        data = None
        if method == "GET":
            url += ("?" + encoded) if encoded else ""
        else:
            data = encoded.encode("ascii")
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": self._auth,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "ClipBridge-Licensing/1",
        })
        try:
            with self._opener(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read().decode("utf-8"))["error"]["message"]
            except Exception:  # noqa: BLE001
                message = f"HTTP {exc.code}"
            raise StripeError(exc.code, str(message)) from exc
        except (OSError, ValueError) as exc:
            raise StripeError(502, f"Stripe に接続できません（{type(exc).__name__}）") from exc

    def get(self, path: str, params: dict | None = None) -> dict:
        return self.request("GET", path, params)

    def post(self, path: str, params: dict | None = None) -> dict:
        return self.request("POST", path, params)


def verify_webhook(payload: bytes, header: str | None, secret: str, now: float | None = None) -> dict:
    """Stripe-Signature ヘッダー（t=...,v1=...）を検証し、イベントを返す。失敗は ValueError。"""
    if not header or not secret:
        raise ValueError("署名がありません")
    timestamp = None
    signatures: list[str] = []
    for part in header.split(","):
        name, _, value = part.strip().partition("=")
        if name == "t":
            timestamp = value
        elif name == "v1":
            signatures.append(value)
    if not timestamp or not timestamp.isdigit() or not signatures:
        raise ValueError("署名の形式が不正です")
    if abs((now if now is not None else time.time()) - int(timestamp)) > WEBHOOK_TOLERANCE:
        raise ValueError("署名の時刻が古すぎます")
    expected = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise ValueError("署名が一致しません")
    try:
        event = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("本文を読めません") from exc
    if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not isinstance(event.get("type"), str):
        raise ValueError("イベントの形式が不正です")
    return event


def sign_webhook(payload: bytes, secret: str, now: float | None = None) -> str:
    """テスト用: Stripe と同じ方法で署名ヘッダーを作る。"""
    timestamp = str(int(now if now is not None else time.time()))
    digest = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"
