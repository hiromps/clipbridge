ClipBridge 1.1.0 — 無料のWindowsアプリ

Windows 10/11の64ビットPC向けです。Pythonのインストールは不要です。

1. ZIPを展開し、ClipBridge.exeをダブルクリックします。
2. 「このPCに保存・デスクトップに追加」を押すと、次回からデスクトップで開けます。
3. 「Tailscale」へ進み、PCとiPhoneにTailscaleを入れて同じアカウントで接続します。
4. 必要に応じて「HTTPS・DNSの設定を開く」でMagicDNSとHTTPSを有効にします。
5. 「HTTPS公開設定を実行 / 修復」を押し、Windowsの管理者確認を許可します。
6. 「iPhone」へ進み、QRコード・接続画面を開きます。
7. iPhoneでQRコードを読み取ってSafariで開き、テスト送信まで進めます。

利用中はClipBridgeを最小化してください。終了するとiPhoneから接続できません。
「診断・更新」から自動起動を設定すると、次回ログオン時に起動できます。

更新
「診断・更新」→「更新を確認する（無料）」から更新できます。
確認後にダウンロードし、サイズとSHA-256を検証してから再起動します。
失敗した場合、現在のアプリと設定はそのままです。
旧版は保存され、「前のインストール版に戻す」で戻せます。
更新情報・ファイルはHTTPSで取得します。現在の配布EXEにコード署名はありません。
Windowsが発行元を確認できない旨を表示する場合があります。

保存先
アプリ：%LOCALAPPDATA%\ClipBridge\versions\バージョン番号\ClipBridge.exe
接続トークン：%LOCALAPPDATA%\ClipBridge\config.json
設定：%LOCALAPPDATA%\ClipBridge\desktop.json
更新時もこれらの接続情報を引き継ぎます。config.jsonは他人へ共有しないでください。

旧Python版から移行する場合
1. 旧版のサーバーを終了し、自動起動タスクを登録していた場合は解除します。
   旧フォルダで powershell -File install_startup.ps1 -Uninstall を実行できます。
2. 新版を起動する前に、旧フォルダのconfig.jsonを
   %LOCALAPPDATA%\ClipBridge\config.json へコピーすると、既存のトークンを引き継げます。
   フォルダがない場合は先に作成します。新旧のconfig.jsonはバックアップしてください。
3. 新版を起動します。8787番ポートが使用中の場合は旧サーバーが残っていないか確認してください。
4. トークンを引き継がなかった場合は、iPhoneとショートカットの接続情報を再設定します。

アンインストール
診断・更新で自動起動を解除し、アプリを終了します。
デスクトップとスタートメニューのClipBridgeショートカット、および
%LOCALAPPDATA%\ClipBridge フォルダを削除します。
Tailscaleの公開設定は他サービスに影響するため、自動では削除しません。

すべての機能は無料です。Tailscaleは別のサービスで、利用条件は同社に従います。
テキスト専用で、コピーの常時自動同期・画像やファイルの転送には対応していません。
サポート・ソース：https://github.com/hiromps/clipbridge
