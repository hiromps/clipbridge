# ClipBridge

iPhoneとWindowsのクリップボードを、スマートに

公開サイト: https://clipbridge-ashen.vercel.app/

ClipBridgeの紹介サイトと、Windows用のソース配布ファイルです。紹介サイトの操作デモはブラウザ内で完結し、実際の端末へ送信しません。

## 紹介サイト

`dist/` にHTML・CSS・JavaScript・画像を配置しています。ビルドは不要です。

```powershell
python -m http.server 8080 --directory dist
```

Vercelでは `vercel.json` の指定により `dist/` を配信します。

## Windows用アプリ

`dist/downloads/ClipBridge-Windows.zip` をダウンロードして展開し、`ClipBridge.exe` を開いてください。Windows 10 / 11の64ビットPCとTailscaleが必要です。**Pythonの個別インストールは不要**です。セットアップ画面の案内で接続し、iPhone側にはTailscaleと、必要に応じて公開版ショートカットを追加します。

アプリのソースは `app/` にあります。基本機能、セットアップ、診断・復旧、更新確認・適用はすべて無料です。配布版にコード署名はなく、Windowsが発行元を確認できない旨を表示する場合があります。

## アプリのビルド・テスト

Windows上でPython、PyInstaller、アプリの依存パッケージを用意して実行します。利用者のPCではPythonは不要です。

```powershell
python -m pip install -r app/requirements.txt pyinstaller pytest
python -m pytest tests -q
python scripts/build_windows.py
```

ビルドは `build/windows/ClipBridge.exe`、配布ZIP、`dist/updates/windows.json` を生成します。ソースだけのZIPは `python scripts/build_download.py` で `build/source/` に生成できます。いずれも個人の認証情報は含めません。

## 無料更新の公開

1. `app/release_info.py` のバージョンを上げて、Windows版をビルド・検証します。
2. GitHubの対応する `vX.Y.Z` リリースに `ClipBridge.exe` とZIP、`SHA256SUMS.txt` を公開します。
3. その後 `dist/updates/windows.json` とLPをVercelへ反映します。先に更新情報だけを公開しないでください。

アプリは公式GitHubリリースのHTTPS URLだけを受け付け、ダウンロードしたファイルのサイズとSHA-256を検証します。取得・検証に失敗した場合は適用しません。更新の適用は利用者の確認後に行い、旧版と接続トークンを保存します。現在の更新検証は公開マニフェストとの一致確認であり、コード署名や独立した署名検証ではありません。

## 移行・復旧

接続データは `%LOCALAPPDATA%\ClipBridge\config.json`、アプリは同フォルダの `versions\バージョン番号` に保存します。診断・更新からサーバー起動、HTTPS修復、旧版への切り替え、自動起動の設定を行えます。既存の別サービスが443番を使用している場合、修復は上書きせず中止します。Tailscaleの管理者許可とアカウントへのログインは利用者が行います。

旧Python版から移行する場合は旧サーバーと自動起動タスクを停止し、必要なら旧 `config.json` を上記保存先へコピーしてから新版を起動します。詳細は `app/WINDOWS-README.txt` を参照してください。

トークンなどの接続情報は各PCで生成します。配布ファイルに個人の接続情報は含まれていません。画像は操作イメージと、サンプルデータを表示した実際のアプリ画面です。

## 注意

テキスト専用です。コピーの常時自動同期、画像やファイルの送受信には対応していません。利用時はPCのClipBridgeが動作し、両端末が同じTailscaleネットワークへ接続している必要があります。
