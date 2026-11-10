#!/usr/bin/env python3
"""ClipBridge Pro のライセンスキーを発行する（販売者用。標準ライブラリのみ）。

鍵は RSA-2048、署名は PKCS#1 v1.5 + SHA-256。アプリ側（app/server.py）は公開鍵だけを持ち、
オフラインで検証する。秘密鍵はリポジトリの外に置き、絶対に公開しないこと。

    python scripts/license_tool.py keygen  --out ~/.clipbridge/license-private.json
    python scripts/license_tool.py issue   --key ~/.clipbridge/license-private.json --email you@example.com --name "山田 太郎"
    python scripts/license_tool.py verify  CBP1.xxxxx.yyyyy

keygen の出力にある公開鍵（n, e）を app/server.py の LICENSE_PUBLIC_KEY に貼る。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
SMALL_PRIMES = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p ** 0.5) + 1, 2))]


def is_probable_prime(n: int, rounds: int = 40) -> bool:
    if n < 2:
        return False
    for p in SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def random_prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if is_probable_prime(candidate):
            return candidate


def keygen(bits: int = 2048) -> dict:
    e = 65537
    while True:
        p, q = random_prime(bits // 2), random_prime(bits // 2)
        if p == q:
            continue
        phi = (p - 1) * (q - 1)
        if phi % e == 0:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        return {"bits": bits, "n": n, "e": e, "d": pow(e, -1, phi)}


def emsa_pkcs1_v15(message: bytes, k: int) -> bytes:
    t = SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    return b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


def rsa_sign(message: bytes, key: dict) -> bytes:
    k = (key["n"].bit_length() + 7) // 8
    em = int.from_bytes(emsa_pkcs1_v15(message, k), "big")
    return pow(em, key["d"], key["n"]).to_bytes(k, "big")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def issue(key: dict, email: str, name: str, license_id: str, plan: str = "pro", expires: str | None = None) -> str:
    payload = {
        "product": "clipbridge",
        "id": license_id,
        "plan": plan,
        "email": email,
        "name": name,
        "issued": time.strftime("%Y-%m-%d"),
        "expires": expires,
        "seats": "unlimited",
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "CBP1." + b64url(body) + "." + b64url(rsa_sign(body, key))


def load_server():
    spec = importlib.util.spec_from_file_location("clipbridge_server", ROOT / "app" / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClipBridge Pro ライセンス発行ツール")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("keygen", help="鍵ペアを作る（1 回だけ）")
    gen.add_argument("--out", type=Path, required=True, help="秘密鍵の保存先（リポジトリの外）")
    gen.add_argument("--bits", type=int, default=2048)
    iss = sub.add_parser("issue", help="ライセンスキーを 1 つ発行する")
    iss.add_argument("--key", type=Path, required=True, help="秘密鍵 JSON")
    iss.add_argument("--email", required=True)
    iss.add_argument("--name", default="")
    iss.add_argument("--id", help="ライセンス ID（省略時は日付 + 連番風の乱数）")
    iss.add_argument("--plan", default="pro")
    iss.add_argument("--expires", help="YYYY-MM-DD（省略時は無期限）")
    ver = sub.add_parser("verify", help="キーをアプリと同じ手順で検証する")
    ver.add_argument("license_key")
    ver.add_argument("--public", type=Path, help="秘密鍵 JSON を渡すと、その公開鍵で検証する（未反映の鍵の確認用）")
    args = parser.parse_args(argv)

    if args.command == "keygen":
        if args.out.exists():
            print(f"すでに存在します: {args.out}（上書きしません）", file=sys.stderr)
            return 1
        key = keygen(args.bits)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({k: (str(v) if isinstance(v, int) and k != "bits" else v) for k, v in key.items()}), encoding="utf-8")
        print(f"秘密鍵を保存しました: {args.out}  ← バックアップして、公開しないこと")
        print("app/server.py の LICENSE_PUBLIC_KEY に貼る値:")
        print(json.dumps({"n": format(key["n"], "x"), "e": key["e"]}, indent=2))
        return 0

    if args.command == "issue":
        raw = json.loads(args.key.read_text(encoding="utf-8"))
        key = {"n": int(raw["n"]), "e": int(raw["e"]), "d": int(raw["d"])}
        license_id = args.id or f"CB-{time.strftime('%Y%m')}-{secrets.token_hex(2).upper()}"
        print(issue(key, args.email, args.name, license_id, args.plan, args.expires))
        return 0

    server = load_server()
    public = None
    if args.public:
        raw = json.loads(args.public.read_text(encoding="utf-8"))
        public = {"n": format(int(raw["n"]), "x"), "e": int(raw["e"])}
    info = server.parse_license(args.license_key, public)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info.get("valid") else 1


if __name__ == "__main__":
    sys.exit(main())
