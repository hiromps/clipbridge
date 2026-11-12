"""ライセンスキー（CBP1.<本文>.<署名>）の署名と検証。RSA-2048 / PKCS#1 v1.5 / SHA-256。

形式は scripts/license_tool.py と app/server.py に合わせる（アプリは公開鍵だけでオフライン検証する）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

PREFIX = "CBP1."
PRODUCT = "clipbridge"
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def load_private_key(raw: str) -> dict:
    """license_tool.py keygen が出力した JSON（n, e, d は 10 進の文字列）を読む。"""
    try:
        data = json.loads(raw)
        key = {"n": int(data["n"]), "e": int(data["e"]), "d": int(data["d"])}
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("LICENSE_PRIVATE_KEY を読めません") from exc
    if key["n"].bit_length() < 2048:
        raise ValueError("LICENSE_PRIVATE_KEY が短すぎます")
    return key


def _encoded(message: bytes, size: int) -> bytes:
    digest_info = SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    return b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info


def issue(key: dict, *, license_id: str, email: str, name: str, expires: str, now: float | None = None) -> str:
    payload = {
        "product": PRODUCT,
        "id": license_id,
        "plan": "pro",
        "email": email,
        "name": name,
        "issued": time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time())),
        "expires": expires,
        "seats": "unlimited",
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    size = (key["n"].bit_length() + 7) // 8
    signature = pow(int.from_bytes(_encoded(body, size), "big"), key["d"], key["n"]).to_bytes(size, "big")
    return PREFIX + b64url(body) + "." + b64url(signature)


def read(key: dict, license_key: object) -> dict | None:
    """自分が署名したキーなら本文を返す。有効期限は見ない（期限切れのキーも更新の本人確認に使う）。"""
    if not isinstance(license_key, str) or len(license_key) > 4096:
        return None
    license_key = license_key.strip()
    if not license_key.startswith(PREFIX) or license_key.count(".") != 2:
        return None
    try:
        _, payload_b64, signature_b64 = license_key.split(".")
        payload = b64url_decode(payload_b64)
        signature = b64url_decode(signature_b64)
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    size = (key["n"].bit_length() + 7) // 8
    if len(signature) != size or not isinstance(data, dict):
        return None
    decrypted = pow(int.from_bytes(signature, "big"), key["e"], key["n"]).to_bytes(size, "big")
    if not hmac.compare_digest(decrypted, _encoded(payload, size)):
        return None
    if data.get("product") != PRODUCT or not isinstance(data.get("id"), str):
        return None
    return data
