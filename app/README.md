# ClipBridge（アプリ本体）

iPhone と Windows PC の間でクリップボードのテキストと画像を受け渡す小さな Web アプリです。
PC 上で動くサーバーに、同じ Tailscale ネットワーク（tailnet）の iPhone から HTTPS でアクセスします。
インターネット上のサーバーは経由しません。

- PC で Ctrl+C → iPhone の画面に即時表示 → 「iPhone にコピー」
- iPhone でコピー → 「PC へ送る」 → PC のクリップボードに入る
- iPhone の写真・画像 → 「写真・画像を選んで送る」 → PC に保存 + クリップボード（または クリップボードだけ）
- PC で画像をコピー（スクリーンショットなど） → iPhone の画面に表示 → 「iPhone にコピー」、長押しで「"写真"に保存」
- 配布版 `ClipBridge.exe` は Python 不要。ソース版は Python 3.10 以上の標準ライブラリのみ

## 入手方法（2 とおり）

| | 配布版（おすすめ） | ソース版（開発・改造向け） |
|---|---|---|
| 入手 | [GitHub Releases](https://github.com/hiromps/clipbridge/releases/latest) の `ClipBridge-Windows.zip` | `git clone https://github.com/hiromps/clipbridge.git` |
| 起動 | `ClipBridge.exe` をダブルクリック | `app\ClipBridge.bat` をダブルクリック（Python 3.10 以上が必要） |
| 保存先 | `%LOCALAPPDATA%\ClipBridge\versions\<版>\`（初回に自動で保存、ショートカット作成） | clone したフォルダー |
| 設定 | `%LOCALAPPDATA%\ClipBridge\config.json` | `app\config.json` |
| アップデート | PC 画面の「アップデートして再起動」（GitHub Releases から取得・検証） | PC 画面の「アップデートして再起動」（`git pull --ff-only`）または手動で `git pull` |

どちらも起動するとブラウザーに **PC セットアップ画面** `http://127.0.0.1:8787/pc` が開きます。

## はじめかた（3 ステップ）

### 1. PC: 一度だけ準備

1. [Tailscale](https://tailscale.com/download) をインストールしてログインする
2. 配布版なら `ClipBridge.exe`、ソース版なら `app\ClipBridge.bat` をダブルクリックする

PC セットアップ画面で Tailscale の状態が自動で確認され、HTTPS 公開（Tailscale Serve）も自動で設定されます。
赤い項目があれば、画面の案内に従ってください。MagicDNS と HTTPS 証明書は Tailscale の管理画面（DNS ページ）で有効にする必要があります。

### 2. iPhone: QR コードを読む

1. iPhone に Tailscale アプリを入れて、PC と同じアカウントでログインし、接続をオンにする
2. PC 画面の **QR コードをカメラで読み取る**（または表示された URL を Safari で開き、6 桁のコードを入力する）

接続されると PC 画面に「iPhone が接続しました」と表示されます。

### 3. iPhone: ホーム画面に追加（おすすめ）

Safari の共有ボタン → 「ホーム画面に追加」。追加したアプリは接続設定を引き継ぐので、コードの再入力は不要です。

## 初回セットアップとショートカット（iPhone）

ペアリングが成功すると、iPhone 側に「初回セットアップ」が開きます（使い方 → ホーム画面に追加 → ショートカット → 完了）。あとから見るには「接続」カードの「セットアップガイド」。

ショートカットを設定すると、iPhone でコピーした文字を **ClipBridge の画面を開かずに** PC へ送れます（背面タップや共有シートに割り当て可能）。

1. ガイドの「ショートカットを追加する」で iCloud の共有ページを開き、「ショートカットを追加」
2. 追加したショートカットを開き、「URL」をガイドに表示された `https://<PC の名前>.<tailnet>.ts.net/api/clip` に、「URL の内容を取得」のヘッダー `X-Token` の値をガイドの「X-Token の値」に置き換える（どちらもコピーボタンあり）
3. iPhone で何か文字をコピーしてショートカットを実行すると、ガイドに「✅ ショートカットから届きました」と出て確認完了

ショートカットの中身は「クリップボードを取得 → テキストを取得 → URL → URL の内容を取得（POST、本文 = テキスト、ヘッダー `X-Token` と `Content-Type: text/plain; charset=utf-8`）」の 4 アクションです。PC ごとに URL とトークンが違うので、PC を増やしたらショートカットを複製して値を変えてください。ショートカットからの送信は iPhone 画面と PC 画面で「ショートカットから」と分かります。

## 複数の PC を使い分ける

iPhone 側は URL（ホーム画面のアイコン）を 1 つのまま、画面上部の PC 名をタップして接続先を切り替えられます。

1. 2 台目以降の PC でも ClipBridge を起動しておく（同じ Tailscale アカウント）
2. iPhone の ClipBridge で、上部の PC 名 →「＋ PC を追加」。同じ tailnet で ClipBridge が動いている PC が一覧に出ます
3. 追加したい PC を選び、その PC のセットアップ画面に表示されている 6 桁コードを入力する

- 一覧に出ない PC は「URL で追加」から、その PC の「iPhone で開く URL」を入力して追加できます
- 切り替えると、表示される PC のクリップボードと「PC へ送る」の送信先がその PC に変わります
- 最初につないだ PC（ハブ）の電源が落ちていても、ホーム画面のアイコンから画面は開けます（Service Worker が画面をキャッシュ）。上部の PC 名から別の PC に切り替えてください
- 仕組み: 各 PC へは iPhone から直接届きます（中継なし）。他の PC の API を呼ぶために、サーバーは同じ tailnet のオリジンからのクロスオリジン呼び出し（CORS）だけを許可し、トークン認証はそのまま必要です。PC 用の管理画面と操作 API はクロスオリジンでは呼べません

## 使い方

| やりたいこと | 操作 |
|---|---|
| PC の文字を iPhone へ | PC で Ctrl+C → iPhone の画面に表示される → 「iPhone にコピー」をタップ |
| iPhone の文字を PC へ | iPhone でコピー → 「iPhone のコピー内容を PC へ送る」→ 「ペースト」をタップ |
| 貼り付け欄から送る | 「ここに貼り付けると自動で送ります」欄に長押しペースト |
| iPhone の写真・画像を PC へ | 「写真・画像を選んで送る」→ 写真ライブラリ / カメラ / ファイルから選ぶ（複数可）。受け取り方は「PC に保存する」（ピクチャ › ClipBridge に保存 + クリップボード）か「クリップボードだけ」（保存せず Ctrl+V で貼り付け）を選べる |
| iPhone でコピーした画像を PC へ | 「iPhone のコピー内容を PC へ送る」（画像なら画像として送る） |
| PC の画像を iPhone へ | PC で画像をコピー（Win+Shift+S のスクリーンショット、ブラウザーの「画像をコピー」、エクスプローラーで画像ファイルを 1 つコピー）→ iPhone の画面に表示 → 「iPhone にコピー」。写真に保存するには画像を長押し →「"写真"に保存」（iOS の仕様で Web ページから直接は保存できないため）。「共有…」で AirDrop や "ファイル" に保存もできる |

iOS の仕様で、iPhone 側のクリップボードを読むときは毎回「ペースト」の確認タップが必要です。
アプリ（Web ページ）を開いている間だけ同期します。バックグラウンドでは動きません。

画像について:

- 対応形式は JPEG / PNG / GIF / BMP。HEIC や WebP は iPhone 側で JPEG（透過がありうるものは PNG）に変換してから送ります。上限は 30 MB
- PC のクリップボードには Windows 標準のビットマップとして入れます（PNG を送ったときは PNG 形式も併せて入れるので、対応アプリでは透過が保たれます）。iPhone の写真の向き（EXIF）は反映します
- 保存先の既定は Windows の「ピクチャ」フォルダー内の `ClipBridge`。変えるには `config.json` に `"image_dir": "D:/ClipBridge"` のように追加して再起動します（PC 画面の「画像の受け渡し」に表示・「保存フォルダーを開く」）
- PC → iPhone は PC のクリップボードが画像になったときだけです。アプリ側に PNG 形式があればそのまま、なければビットマップを PNG に変換して送ります

## ClipBridge Pro（有料機能）

無料版はテキストと画像の双方向、PC 2 台までを含みます。Pro は **1 人分のライセンスで PC 何台でも** 使えます。

| 機能 | 内容 |
|---|---|
| 3 台目以降の PC | iPhone に 3 台目以降の PC を登録できます（追加する PC か、登録済みのどれかの PC で Pro が有効なこと） |
| クリップボードの履歴 | 直近 100 件（テキストと画像）を PC に保存。iPhone と PC 画面から検索・PC のクリップボードへ戻す（再送）・ピン留め（上限に数えず消去でも残る）・削除。記録のオン / オフ可 |
| 画像の保存ルール | サブフォルダー（なし / 日付 / 月）と、ファイル名のテンプレート（`{date}` `{time}` `{datetime}` `{year}` `{month}` `{day}` `{name}` `{pc}`） |
| PC 側ホットキー（Windows） | 「セットアップ画面を開く」「iPhone から最後に届いた内容をクリップボードに戻す」「履歴を 1 つ前へ（押すたびに遡る）」。既定は Ctrl+Alt+C / Ctrl+Alt+V / Ctrl+Alt+Left、変更可 |
| 優先サポート | PC 画面の「優先サポート」からライセンス ID つきで問い合わせ |

- 適用: PC セットアップ画面の「ClipBridge Pro」にキーを貼り付けて「ライセンスを適用」。キーは `config.json` に保存されます。入力は **1 台だけ** で足ります
- ほかの PC への配布: iPhone の ClipBridge を開くと、Pro が有効な PC のキーを、登録済みのほかの PC（未適用、または同じ契約で期限が古いもの）へ自動で届けます。受け取った PC が署名を検証してから適用し、別の契約が有効な PC は置き換えません。キーは iPhone には保存しません（iPhone 用 API `GET /api/license` / `POST /api/license`、トークン認証）
- 仕組み: キーは RSA-2048 で署名された JSON（`CBP1.<本文>.<署名>`）。アプリは公開鍵だけを持ち、オフラインで検証します
- サブスクリプション（有効期限つきのキー）: 起動 15 秒後と 6 時間ごとに確認し、1 日 1 回まで発行サーバー（`LICENSE_REFRESH_URL`）へ問い合わせて、契約が続いていれば期限を延ばしたキーに自動で差し替えます。送るのは **キー・この PC のランダムな ID（`config.json` の `install_id`）・PC 名・版** だけで、クリップボードの内容は送りません。通信できなくても期限までは使えます。期限が切れてもキーは残り、契約が続いていれば次の確認（または PC 画面の「契約を確認して更新」）で元に戻ります。解約後は期限まで使え、返金などで取り消されたキーはその場で外れます
- 無期限のキーは発行サーバーへ通信しません
- 発行サーバーとの取り決め: `POST` に JSON `{key, install_id, pc_name, version}`。継続中は `200 {"key": "CBP1...."}`（同じライセンス ID）、契約終了は `402/403/410 {"error": "<理由>", "message": "<表示文>"}`（`error` が `revoked` のときだけ即時に無効化）。それ以外の応答は一時的な失敗として扱います
- 履歴の保存先: 配布版は `%LOCALAPPDATA%\ClipBridge\history.json` と `history\`（画像）、ソース版は `app/history.json`（gitignore 済み）。クリップボードの内容がそのまま入るので共有しないでください
- 発行（販売者側）: `python scripts/license_tool.py keygen --out <リポジトリ外>` で鍵ペアを 1 回作り、公開鍵を `server.py` の `LICENSE_PUBLIC_KEY` に貼ります。以後は `python scripts/license_tool.py issue --key <秘密鍵> --email <購入者>` でキーを発行、`verify` で確認できます。秘密鍵は絶対に公開しないでください

## PC セットアップ画面でできること

- **PC のクリップボード**: 今の内容（文字 / 画像のプレビュー、文字数、送信元、更新時刻）を iPhone 画面と同じように表示。iPhone から届くと「iPhone から届きました」のバッジ。入力欄から PC → iPhone へ文字を送ることもできます（Ctrl+Enter で送信）
- Tailscale のチェック（インストール / 接続 / MagicDNS / HTTPS 証明書 / Serve / Funnel オフ）と修復ボタン
- 接続用 QR コード・URL・6 桁ペアリングコード（1 回使うたびに更新。5 回間違えても更新）
- 接続してきた端末の一覧（最終アクセス時刻）
- **画像の受け渡し**: 保存先の変更と「保存フォルダーを開く」、最後に受け取った画像、保存ルール（Pro）
- **ClipBridge Pro**: ライセンスの適用・解除、サブスクリプションの残り日数と「契約を確認して更新」、履歴（検索・戻す・ピン留め・消去）、ホットキーの設定、優先サポートへの導線
- **アップデート**: 起動 10 秒後と 6 時間ごとに新しい版を確認し、「アップデートして再起動」で適用（下記）
- Windows サインイン時の自動起動（スタートアップフォルダーに `ClipBridge.vbs` を置く。管理者権限不要）
- すべての iPhone の接続解除（トークン再発行）、サーバー停止

コマンドラインからも操作できます（配布版は `ClipBridge.exe` に同じ引数を渡せます）。

```powershell
python server.py                   # 起動してセットアップ画面を開く
python server.py --no-browser      # 画面を開かずに起動
python server.py --startup on      # 自動起動を登録（off で解除）
python server.py --port 8790       # ポート変更（Tailscale Serve の転送先も合わせて再設定されます）
python server.py --no-update-check # 更新の自動確認をしない
python server.py --no-serve        # Tailscale Serve を自動設定しない（開発・テスト用）
```

## アップデートの仕組み

- **配布版**: `https://github.com/hiromps/clipbridge/releases/latest/download/windows.json` を読み、版が新しければ
  「アップデートして再起動」を表示します。押すと GitHub Releases の `ClipBridge.exe` をダウンロードし、
  マニフェストのサイズと SHA-256 を検証してから `%LOCALAPPDATA%\ClipBridge\versions\<新しい版>\` に保存し、
  ショートカットを付け替えて再起動します。設定（トークン）は引き継がれ、旧版の exe も残ります。
  マニフェストの URL は公式リポジトリの Releases 以外を受け付けません。
- **ソース版（git clone）**: `origin` の同じブランチに新しいコミットがあれば表示し、`git pull --ff-only` のあと同じ引数で再起動します。
  ローカルに未コミットの変更があると中止します。
- 旧版（1.x）の exe からは、旧版の「診断・更新」→「更新を確認する」で 2.0 に更新できます（保存先とトークンは共通）。

## セキュリティ

- サーバーは `127.0.0.1` だけで待ち受け、外からは Tailscale Serve（tailnet 内限定の HTTPS）経由のみ届きます
- iPhone 用 API はトークン認証です。トークンは `config.json` に保存されます（共有しないでください）
- PC 用の管理画面と操作 API は、ループバックから直接来たリクエストで、かつ `Host` が自分自身のときだけ応答します（DNS リバインディング・CSRF 対策）
- ペアリングコードは 6 桁・使い捨て・5 回失敗で更新
- クリップボードの内容、トークン、コードはログに出力しません
- Funnel（インターネット公開）が有効になっていると画面で警告します
- 更新は HTTPS で取得し、サイズと SHA-256 を検証します（コード署名はありません）

## ファイル構成

```
app/
├── ClipBridge.bat        ソース版をダブルクリックで起動
├── server.py             サーバー本体（標準ライブラリのみ。exe もこのファイルから作る）
├── WINDOWS-README.txt    配布 ZIP に同梱する「はじめに.txt」
├── ClipBridge.ico        exe のアイコン
├── web/
│   ├── index.html, app.js    iPhone 用画面
│   ├── pc.html, pc.js        PC 用セットアップ画面
│   ├── style.css, icon-*.png
│   ├── img/*.webp            説明用の図解（Codex CLI の画像生成で作成、WebP 幅 1200px）
│   └── vendor/qrcode.js      QR 生成（qrcode-generator, MIT）
├── tests/test_server.py  結合テスト（pytest）
└── tools/make_icons.py   アイコン生成（開発用、Pillow）
```

## テスト

リポジトリのルートで実行します。

```powershell
pip install -r app/requirements-dev.txt
python -m pytest
```

テストはクリップボードと Tailscale と git を差し替えて実行します（Windows では実クリップボードの往復テストも 1 件走り、元の内容を復元します）。

## トラブルシュート

- **QR が表示されない / 「HTTPS 公開」が赤い**: Tailscale 管理画面の DNS ページで MagicDNS と HTTPS Certificates を有効にし、「再確認」を押します。`tailscale serve` が「Access is denied」になる場合は、管理者として開いた PowerShell で画面に表示されたコマンドを実行してください。
- **iPhone で「PC に接続できません」**: iPhone の Tailscale がオンか、PC で ClipBridge が起動しているか（PC 画面が開くか）を確認します。
- **コードが違うと言われる**: コードは 1 回使うと変わります。PC 画面の最新のコードを入力してください。
- **「新着」が出ない**: アプリを開き直すと再接続します。PC 側で `tailscale serve status` が ClipBridge のポートを指しているか確認してください。
- **443 を別のサービスで使っている**: 空いている HTTPS ポート（8443 など）を自動で選びます。URL は `https://<PC名>.<tailnet>.ts.net:8443/` のようになります。
- **更新情報を取得できない**: GitHub に届かない環境では確認に失敗しますが、本体の動作には影響しません。手動で Releases から ZIP を取得して exe を開けば、同じ保存先に新しい版が入ります。

## アンインストール

1. PC 画面で自動起動をオフにし、「サーバーを停止する」を押す
2. Tailscale Serve の設定を外す: `tailscale serve --https=443 off`（他のサービスで Serve を使っている場合は、そのポートだけを外してください）
3. 配布版はデスクトップとスタートメニューの `ClipBridge` ショートカットと `%LOCALAPPDATA%\ClipBridge` フォルダーを、ソース版は clone したフォルダーを削除する（`config.json` にトークンが入っています）
