ClipBridge 2 — iPhone と Windows のクリップボード共有（無料）

Windows 10 / 11（64 ビット）向けです。Python のインストールは不要です。

■ はじめかた
1. ZIP を展開し、ClipBridge.exe をダブルクリックします。
   初回はアプリが %LOCALAPPDATA%\ClipBridge に保存され、デスクトップとスタートメニューに
   「ClipBridge」のショートカットができます。次回からはそのショートカットから開けます。
2. ブラウザーに「PC セットアップ画面」が開きます（http://127.0.0.1:8787/pc）。
   Tailscale の状態が自動で確認され、HTTPS 公開（Tailscale Serve）も自動で設定されます。
   赤い項目があれば画面の案内に従ってください（Tailscale のインストールとログイン、
   Tailscale 管理画面での MagicDNS と HTTPS 証明書の有効化）。
3. iPhone に Tailscale アプリを入れ、PC と同じアカウントでログインして接続をオンにします。
4. PC 画面の QR コードを iPhone のカメラで読み取ります
   （または表示された URL を Safari で開き、6 桁のコードを入力します）。
5. Safari の共有ボタン →「ホーム画面に追加」で、アプリのように使えます。

■ 使い方
・PC で Ctrl+C → iPhone の画面にすぐ表示 →「iPhone にコピー」をタップ
・iPhone でコピー →「iPhone のコピー内容を PC へ送る」→「ペースト」をタップ
ClipBridge の画面を開いている間だけ同期します（iOS の仕様でバックグラウンドでは動きません）。

■ 画像
・iPhone → PC: 「写真・画像を選んで送る」で写真ライブラリやカメラから選びます。
  「PC に保存する」（ピクチャ\ClipBridge に保存 + クリップボード）か「クリップボードだけ」
  （保存せず Ctrl+V で貼り付け）かを iPhone 側で選べます。iPhone でコピーした画像は
  「iPhone のコピー内容を PC へ送る」でも送れます。
・PC → iPhone: PC で画像をコピー（Win+Shift+S のスクリーンショットなど）すると iPhone の画面に
  表示され、「iPhone にコピー」が使えます。写真に保存するには画像を長押しして「"写真"に保存」を
  選びます（「共有…」で AirDrop や "ファイル" への保存もできます）。
・保存先は PC セットアップ画面の「画像の受け渡し」に表示されます（「保存フォルダーを開く」）。
  変更するには config.json に "image_dir": "D:/ClipBridge" のように追加して再起動します。

■ 複数の PC で使う
他の PC にも ClipBridge を入れて起動しておくと、iPhone 側はアイコン 1 つのまま切り替えられます。
iPhone の ClipBridge で上部の PC 名をタップ →「＋ PC を追加」→ 一覧から PC を選び、
その PC の画面に表示されている 6 桁コードを入力します。以後は上部の PC 名から切り替えます。

■ ClipBridge Pro（有料機能）
無料版はテキストと画像の双方向、PC 2 台まで。Pro は 1 人分のライセンスで PC 何台でも使えます。
・3 台目以降の PC を iPhone に登録
・クリップボードの履歴 100 件（検索・PC のクリップボードへ戻す・ピン留め）を iPhone と PC 画面から
・画像の保存ルール（日付フォルダー、ファイル名のテンプレート）
・PC 側ホットキー（画面を開く / iPhone から届いた内容を戻す / 履歴を遡る）
・優先サポート
PC セットアップ画面の「ClipBridge Pro」にキーを貼り付けて「ライセンスを適用」します。

■ 自動起動
PC セットアップ画面の「Windows にサインインしたら自動で起動する」をオンにします。

■ アップデート
PC セットアップ画面の「アップデート」に新しいバージョンが表示されたら
「アップデートして再起動」を押します。GitHub Releases から取得し、サイズと SHA-256 を
検証してから入れ替えます。接続設定（トークン）は引き継がれ、旧版の exe も残ります。
更新情報: https://github.com/hiromps/clipbridge/releases

■ 保存先
アプリ: %LOCALAPPDATA%\ClipBridge\versions\<バージョン>\ClipBridge.exe
設定（接続トークン）: %LOCALAPPDATA%\ClipBridge\config.json  ※他人に共有しないでください

■ 旧版（1.x）からの更新
旧版の「診断・更新」→「更新を確認する（無料）」から 2.0 に更新できます。
トークンと保存先はそのまま引き継がれます。画面とペアリング方法が変わるため、
iPhone 側は PC 画面の QR コードでもう一度接続してください。

■ アンインストール
PC 画面で自動起動をオフにして「サーバーを停止する」を押し、デスクトップとスタートメニューの
ショートカットと %LOCALAPPDATA%\ClipBridge フォルダーを削除します。
Tailscale の公開設定を外す場合: tailscale serve --https=443 off

■ 注意
テキストと画像に対応しています。画像以外のファイルの転送、常時のバックグラウンド同期には対応していません。
配布版にコード署名はないため、Windows が発行元を確認できない旨を表示する場合があります。
サポート・ソース: https://github.com/hiromps/clipbridge
