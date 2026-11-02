# ClipBridge

この文書はPythonから実行するソース版向けです。通常のWindows利用ではPythonは不要です。配布ZIPの `ClipBridge.exe` を開いてセットアップ画面に従ってください。無料の診断・復旧・更新機能、旧版からの移行手順は `WINDOWS-README.txt` にあります。

PCではデスクトップの「ClipBridge」または `ClipBridge.bat` から開いてください。起動時にTailscaleのHTTPS接続を確認し、利用可能ならiPhoneと同じHTTPSアドレスを認証情報付きで開きます。利用できない場合はPCローカル画面を開きます。HTTPSとローカル画面、別のブラウザ、Safariとホーム画面版では認証情報の保存先が異なるため、それぞれ初回の設定が必要です。

PCでClipBridgeの「接続管理」タブを開くと、「iPhoneを接続する」に認証情報付きのQRコードとセットアップURLが表示されます。iPhoneのTailscaleを接続し、カメラでQRコードを読み取ってSafariで開いてください。「セットアップURLをコピー」からURLをコピーすることもできます。ホーム画面版で接続情報を求められた場合は、Safariの「接続管理」にある同じボタンでURLをコピーし、ホーム画面版の接続設定へ貼り付けます。接続情報がない場合は接続管理が開き、設定が完了するとクリップボード画面へ戻ります。URLには認証情報が含まれるので他人へ共有しないでください。

iPhone と Windows PC の間で**テキストだけ**を受け渡す小さな Web アプリです。PC 上の Python サーバーに、同じ Tailscale ネットワークに参加した iPhone から HTTPS でアクセスします。

## 事前準備

初回の画面では「3ステップの接続案内を開く」から、接続確認・テスト送信・完了まで進めます。「テスト送信」は入力した文字をPCのクリップボードへ書き込み、読み戻して一致を確認します。現在のコピー内容が置き換わるため、必要な内容は先に貼り付けて保存してください。設定完了後は初回案内が非表示になり、接続管理から再度テストできます。

画面上部に接続先PC名と接続状態が表示されます。「接続を確認」ではPCのTailscale導入・接続状態とClipBridgeのHTTPS公開設定を確認します。通信そのものが届かないときは原因を断定せず、PCの起動・ClipBridge・Tailscaleの確認手順を表示します。iPhone側のTailscale導入・VPN接続状態はブラウザから自動判定できません。

Tailscaleが未導入の場合は、接続管理の「iPhone用 · App Store」または「Windows用をダウンロード」からインストールします。両端末で同じアカウントのネットワークに接続してください。iPhoneのVPN構成追加を許可して接続をオンにしてから、セットアップQRコードを読み取ります。

接続管理の準備チェックでは、iPhoneへのインストール・PCへのインストール・ログインと接続・PCの公開設定を個別に完了にできます。チェックした項目は説明やダウンロードリンクごと非表示になり、状態はその端末のブラウザに保存されます。「完了した項目を表示」で見直し、チェックを外すと説明を再表示できます。チェックは利用者の記録であり、接続テストの診断結果とは別です。

PCでは「iPhoneへ共有する」「PCの内容をコピー」、iPhoneでは「PCへ送る」「iPhoneへ取る」のように操作端末に合わせて表示します。PCから共有した内容は、iPhone側の「iPhoneへ取る」で取得します。送受信に成功すると、文字数と操作時刻が画面に表示されます。コピーをブラウザが許可しない場合は手動コピー・貼り付けの案内が表示されます。

セットアップQRコードから開くと接続管理が表示され、公開版「CopyBridge-公開用」の追加リンク、送信先URL・接続トークンのコピーボタンを利用できます。追加はiPhone側で確認し、ショートカット内のURLとX-Tokenを設定します。コントロールセンターへの登録もiPhone側で行います。「クリップボードへ進む」でセットアップ表示を終了できます。通常の起動ではクリップボード画面が表示されます。個人のトークンを含めたショートカットは公開しないでください。

- Windows に Python 3.10 以上と Tailscale をインストールし、Tailscale にログインします。
- iPhone にも Tailscale をインストールし、同じ tailnet にログインします。
- Tailscale の管理画面で **MagicDNS** と **HTTPS Certificates** を有効にします。
- PC で `python --version` と `tailscale status` が使えることを確認します。

## セットアップ

PowerShell でこのディレクトリに移動し、次を実行します。

```powershell
pip install -r requirements.txt
python make_icon.py
python server.py
```

初回起動で `config.json` とランダムなトークンが生成されます。ファイルは外部へ共有しないでください。サーバーは `127.0.0.1:8787` のみで待ち受けます。コンソールの `セットアップURL` を控えます。MagicDNS 名を取得できない場合は `<MagicDNS名>` と表示されるため、`tailscale status --json` の `Self.DNSName` を確認して置き換えてください。

PyPI に接続できず `pyperclip` を導入できない Windows 環境でも、サーバーは標準ライブラリの Windows クリップボード API を使って動作します。ただし、正式な自動テストには `pytest` の導入が必要です。

別の PowerShell で以下を実行します。スクリプトは利用中の Tailscale の `serve --help` を確認し、HTTPS のポート443でバックグラウンドの Serve を設定して状態を表示します。

```powershell
.\setup_tailscale.ps1
```

Tailscale の管理者パイプへのアクセス拒否が出る場合は、PowerShell を管理者として開いて再実行してください。PowerShell の実行ポリシーで止まる場合は `powershell -ExecutionPolicy Bypass -File .\setup_tailscale.ps1` とします。iPhone の Safari で **サーバーが表示した** `https://<MagicDNS名>/#t=<token>` を開きます。`#t=` 以降のトークンは初回に端末内へ保存され、アドレスバーから消えます。この URL は認証情報を含むため他人へ送らないでください。

画面の「📤 PCへ送る」で iPhone のコピー内容を PC に送れます。「📥 iPhoneへ取る」で PC のコピー内容を iPhone に保存します。PC の現在の内容は画面が表示されている間、約1.5秒ごとに更新されます。

## ホーム画面に追加

Safari の共有ボタンから「ホーム画面に追加」を選びます。追加した ClipBridge を開いて操作します。初回は必ずトークン付きセットアップURLを Safari で開いてください。**ホーム画面版は Safari と localStorage を共有しない**ため、初回起動で「接続用の情報がありません」と出ることがあります。その場合はホーム画面版の「セットアップURL またはトークン」欄に、PC のコンソールに表示されたセットアップURL全体を貼り付けて「接続する」を押してください。ホーム画面版に保存されるので、次回から入力は不要です。401 の場合も同じ欄から設定し直せます。URL はトークンを含むため、他人へ共有しないでください。

## 常駐化とアンインストール

Windows でワンクリック起動する場合は、このフォルダーの `ClipBridge.bat` を一度ダブルクリックします。デスクトップに `ClipBridge` ショートカットが作成され、以後はそのショートカットをクリックするだけで、必要ならサーバーを起動してブラウザーを開きます。`ClipBridge.lnk` をデスクトップへコピーしても同じです。すでにサーバーが動いている場合は二重起動しません。デスクトップのショートカットを削除すれば、この起動方法だけを解除できます。

`python server.py` が動作することを確認後、PowerShell で次を実行するとログオン時に `pythonw.exe server.py` を起動するタスク `ClipBridge` が登録されます。再実行で更新されます。

```powershell
.\install_startup.ps1
```

タスク登録後も、その場でサーバーを起動するには `python server.py` を実行するか、次回ログオンしてください。アンインストールする場合は次を実行し、起動中のサーバーを停止してください。ClipBridge のためだけに Tailscale Serve を設定した PC では、Serve 設定も解除できます。`tailscale serve reset` はその PC の Serve 設定をすべて消すため、他のサービスを公開している場合は実行せず、設定を個別に見直してください。

```powershell
Unregister-ScheduledTask -TaskName ClipBridge -Confirm:$false
tailscale serve reset
```

その後、このディレクトリを削除できます。`config.json` はトークンを含むため安全に管理してください。

## テスト

```powershell
pytest
```

テストは `pyperclip.copy/paste` を差し替えた HTTP 通信で実行します。実際の PC クリップボードでの確認は、サーバー起動後に次の例を使用できます。`config.json` は外部へ出さないでください。

```powershell
$token = (Get-Content .\config.json -Raw | ConvertFrom-Json).token
curl.exe -i http://127.0.0.1:8787/api/health
curl.exe -i -H "X-Token: $token" -H "Content-Type: text/plain; charset=utf-8" --data-binary "ClipBridge確認" http://127.0.0.1:8787/api/clip
curl.exe -i -H "X-Token: $token" http://127.0.0.1:8787/api/clip
```

最初のコマンドは 401、後のコマンドは送信と取得に成功するはずです。

## トラブルシュート

- **証明書エラー**: Tailscale 管理画面の MagicDNS と HTTPS Certificates を確認します。端末が tailnet に接続されているか、DNS 名が正しいかも確認します。
- **401**: セットアップURLからもう一度開いてください。`config.json` を作り直した場合、古いトークンは使えません。
- **タイルから接続できない**: PC のサーバーが動いているか、`tailscale serve status` が HTTPS 443 を示すか、PC と iPhone の Tailscale が接続中かを確認します。
- **貼り付け確認が出る**: iOS Safari の `readText()` は実行のたびに「ペースト」確認の吹き出しが出ます。つまり **1タップ + ペースト許可1タップ** になります。これは iOS の仕様で回避できません。
- **直接コピーや読み取りが失敗する**: 画面に表示される手動貼り付け・長押しコピーの欄を使ってください。

**バックグラウンドでの自動同期はできません。** アプリ (PWA) を開いている間だけ動きます。「開いたとき自動で送る」を有効にしても、iOS のペースト許可が必要です。

## 実機での手動確認チェックリスト

- [ ] iPhone の Tailscale を接続し、Safari でセットアップURLを開ける。
- [ ] iPhone でテキストをコピーし「PCへ送る」を押して、PC で貼り付けできる。
- [ ] PC でテキストをコピーし「iPhoneへ取る」を押して、iPhone で貼り付けできる。
- [ ] Safari からホーム画面に追加し、追加したアプリでも操作できる。
