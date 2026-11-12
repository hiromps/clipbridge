# ClipBridge

iPhone と Windows のクリップボードを、スマートに。

公開サイト: https://clipbridge-ashen.vercel.app/ ／ 配布: https://github.com/hiromps/clipbridge/releases

このリポジトリには紹介サイト（`dist/`）と Windows アプリ本体（`app/`）が入っています。

```
app/        アプリ本体（server.py と web/）。exe もここから作る → 使い方は app/README.md
dist/       紹介サイト（静的 HTML）。Vercel が配信する。dist/updates/windows.json は旧版 1.x 向けの更新情報
api/        Vercel Functions（Python）。ClipBridge Pro の購入・ライセンス発行・更新
licensing/  発行サーバーの本体（api/ の各ファイルから使う）
db/         Neon（Postgres）のマイグレーション
scripts/    build_windows.py（exe・ZIP・更新マニフェスト）、build_download.py（ソース ZIP）、
            license_tool.py（キーの手動発行）、stripe_setup.py（Stripe の商品・価格・webhook）
tests/      発行サーバーのテスト
.github/    windows-build.yml（テスト → ビルド → 検証 → タグなら GitHub Release）
```

## 使う人向け

1. [Releases](https://github.com/hiromps/clipbridge/releases/latest) から `ClipBridge-Windows.zip` を取得して展開し、`ClipBridge.exe` を開く
2. ブラウザーに開く PC セットアップ画面の案内に従って Tailscale を接続し、iPhone で QR コードを読む

詳しい手順と仕組みは [`app/README.md`](app/README.md)、配布 ZIP 同梱の `はじめに.txt`（`app/WINDOWS-README.txt`）を参照してください。
Python で動かす場合は `git clone` して `app\ClipBridge.bat` を実行します。

## 開発

```powershell
python -m pip install -r app/requirements-dev.txt pyinstaller
python -m pytest                      # テスト（リポジトリのルートで）
python scripts/build_windows.py       # build/windows/ に ClipBridge.exe, ClipBridge-Windows.zip, windows.json, SHA256SUMS.txt
python scripts/build_download.py      # build/source/ClipBridge-Source.zip
python -m http.server 8080 --directory dist   # 紹介サイトをローカルで確認
```

## ClipBridge Pro の発行サーバー

月額 480 円 / 年額 4,800 円（税込、14 日間の無料トライアル）のサブスクリプションです。
Stripe で購入 → Neon に契約を保存 → 署名したライセンスキーを発行、という流れを Vercel Functions（`api/`）で行います。

| エンドポイント | 役割 |
|---|---|
| `POST /api/checkout`（フォームの `plan=monthly\|yearly`） | Stripe の決済ページへ移動する |
| `POST /api/stripe/webhook` | Stripe の通知を受けて契約の状態を保存する（署名検証・二重処理の防止） |
| `GET /api/license/claim?session_id=...` | 購入直後のページ。ライセンスキーを表示する |
| `POST /api/license/refresh` | アプリからの更新確認。契約が続いていれば期限を延ばしたキーを返す |
| `POST /api/license/portal` | キーを見せると、解約・カード変更ページ（Stripe カスタマーポータル）の URL を返す |

キーの有効期限は「支払い済みの期間の終わり + 猶予 14 日」です（トライアル中は + 1 日、解約予約中は猶予なし）。
全額返金（`charge.refunded`）でライセンスは取り消され、アプリは次の確認でキーを外します。

### 初回の設定

1. **DB**: Neon のプロジェクト `clipbridge` に `db/migrations/` を番号順に適用する（適用済み）
2. **Stripe**: 商品・価格・webhook を作る。まずテスト用のキーで試す

   ```powershell
   $env:STRIPE_SECRET_KEY = "sk_test_..."
   python scripts/stripe_setup.py
   ```

   続けて Stripe ダッシュボードの「設定 › Billing › カスタマーポータル」を開いて保存する
3. **Vercel の環境変数**（Production）

   | 変数 | 値 |
   |---|---|
   | `DATABASE_URL` | Neon の接続文字列（プール接続） |
   | `LICENSE_PRIVATE_KEY` | `license_tool.py keygen` が作った秘密鍵の JSON（`app/server.py` の公開鍵と対になるもの） |
   | `STRIPE_SECRET_KEY` | Stripe のシークレットキー |
   | `STRIPE_WEBHOOK_SECRET` | `stripe_setup.py` が表示した署名シークレット（`whsec_...`） |
   | `SITE_URL` | 公開サイトの URL（既定の `https://clipbridge-ashen.vercel.app` から変えるときだけ） |

4. 環境変数を変えたら再デプロイする（設定前は各エンドポイントが 503「準備中」を返す）

サイトの URL を変えるときは、`app/server.py` の `SITE_URL`（配布した exe に入る）、Stripe の webhook の URL、`SITE_URL` 環境変数をそろえて変えます。

## リリース手順（他の PC に最新版を配る）

1. `app/server.py` の `__version__` を上げて（例 `2.1.0`）コミットする
2. タグを付けて push する

   ```powershell
   git tag v2.1.0
   git push origin main --tags
   ```

3. GitHub Actions がテスト → exe ビルド → 同梱 self-test → **Release `v2.1.0`** を作る（`ClipBridge.exe`, `ClipBridge-Windows.zip`, `SHA256SUMS.txt`, `windows.json`）

これだけで、各 PC の ClipBridge が `releases/latest/download/windows.json` を読んで「新しいバージョン」を表示し、
「アップデートして再起動」で入れ替わります（サイズと SHA-256 を検証）。git clone で動かしている PC は同じボタンで `git pull` されます。
タグとバージョンが一致しないとワークフローが失敗します。

- 紹介サイトのダウンロードボタンは `releases/latest/download/ClipBridge-Windows.zip` を指すので、リリースのたびに更新する必要はありません
- LP（`dist/index.html`）にも版が書いてあります。リリース時にヘッダーのピル `v2.2.0 ・ 無料` と JSON-LD の `softwareVersion`、`dist/sitemap.xml` の `lastmod` を更新してください
- `dist/updates/windows.json` は **旧版 1.x だけ** が読む更新情報です。1.x の PC が残っている間は、Release 作成後に
  Release の `windows.json` をそのままコピーして push します（Vercel が自動で配信します）

  ```powershell
  gh release download v2.1.0 -p windows.json -O dist/updates/windows.json
  git commit -am "Point legacy update feed at 2.1.0" ; git push
  ```

## ClipBridge Pro（有料機能）

無料版はテキストと画像の双方向・PC 2 台まで。Pro（1 人分のライセンスで PC 何台でも）で、3 台目以降の PC、クリップボードの履歴 100 件（検索・再送・ピン留め）、画像の保存ルール、PC 側ホットキー、優先サポートが使えます。
ライセンスキーは `scripts/license_tool.py` で発行し（秘密鍵はリポジトリの外に置く）、アプリは埋め込んだ公開鍵でオフライン検証します。詳細は `app/README.md` の「ClipBridge Pro」を参照。

## 安全性

- サーバーは `127.0.0.1` だけで待ち受け、iPhone からは Tailscale Serve（tailnet 内限定の HTTPS）経由で届きます
- 更新は公式 Releases の HTTPS URL だけを受け付け、ダウンロード後にサイズと SHA-256 を検証してから入れ替えます。配布版にコード署名はありません
- 接続トークンは各 PC の `%LOCALAPPDATA%\ClipBridge\config.json`（ソース版は `app/config.json`）に生成され、配布物には含まれません

## 注意

テキストと画像を扱います（画像は iPhone → PC が「保存 + クリップボード」または「クリップボードのみ」、PC → iPhone が表示・コピー・写真への保存）。コピーの常時自動同期、画像以外のファイルの送受信には対応していません。利用時は PC の ClipBridge が動作し、両端末が同じ Tailscale ネットワークへ接続している必要があります。
