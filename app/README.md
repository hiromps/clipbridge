# ClipBridge（アプリ本体）

iPhone と Windows PC の間でクリップボードのテキストを受け渡す小さな Web アプリです。
PC 上で動くサーバーに、同じ Tailscale ネットワーク（tailnet）の iPhone から HTTPS でアクセスします。
インターネット上のサーバーは経由しません。

- PC で Ctrl+C → iPhone の画面に即時表示 → 「iPhone にコピー」
- iPhone でコピー → 「PC へ送る」 → PC のクリップボードに入る
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

## 使い方

| やりたいこと | 操作 |
|---|---|
| PC の文字を iPhone へ | PC で Ctrl+C → iPhone の画面に表示される → 「iPhone にコピー」をタップ |
| iPhone の文字を PC へ | iPhone でコピー → 「iPhone のコピー内容を PC へ送る」→ 「ペースト」をタップ |
| 貼り付け欄から送る | 「ここに貼り付けると自動で送ります」欄に長押しペースト |

iOS の仕様で、iPhone 側のクリップボードを読むときは毎回「ペースト」の確認タップが必要です。
アプリ（Web ページ）を開いている間だけ同期します。バックグラウンドでは動きません。

## PC セットアップ画面でできること

- Tailscale のチェック（インストール / 接続 / MagicDNS / HTTPS 証明書 / Serve / Funnel オフ）と修復ボタン
- 接続用 QR コード・URL・6 桁ペアリングコード（1 回使うたびに更新。5 回間違えても更新）
- 接続してきた端末の一覧（最終アクセス時刻）
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
