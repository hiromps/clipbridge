# ClipBridge

iPhoneとWindowsのクリップボードを、スマートに

ClipBridgeの紹介サイトと、Windows用のソース配布ファイルです。紹介サイトの操作デモはブラウザ内で完結し、実際の端末へ送信しません。

## 紹介サイト

`dist/` にHTML・CSS・JavaScript・画像を配置しています。ビルドは不要です。

```powershell
python -m http.server 8080 --directory dist
```

Vercelでは `vercel.json` の指定により `dist/` を配信します。

## Windows用アプリ

`dist/downloads/ClipBridge-Windows.zip` をダウンロードし、展開後のREADMEに従ってセットアップしてください。Windows、Python 3.10以上、Tailscaleが必要です。iPhoneにはTailscaleと、必要に応じて公開版ショートカットを追加します。

アプリのソースは `app/` にあります。配布用ZIPは、認証情報を除外する `python scripts/build_download.py` で再生成できます。

トークンなどの接続情報は各PCで生成します。配布ファイルに個人の接続情報は含まれていません。画像は操作イメージと、サンプルデータを表示した実際のアプリ画面です。

## 注意

テキスト専用です。コピーの常時自動同期、画像やファイルの送受信には対応していません。利用時はPCのClipBridgeが動作し、両端末が同じTailscaleネットワークへ接続している必要があります。
