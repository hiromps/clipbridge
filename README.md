# ClipBridge

iPhone と Windows のクリップボードを、スマートに。

公開サイト: https://clipbridge-ashen.vercel.app/ ／ 配布: https://github.com/hiromps/clipbridge/releases

このリポジトリには紹介サイト（`dist/`）と Windows アプリ本体（`app/`）が入っています。

```
app/        アプリ本体（server.py と web/）。exe もここから作る → 使い方は app/README.md
dist/       紹介サイト（静的 HTML）。Vercel が配信する。dist/updates/windows.json は旧版 1.x 向けの更新情報
scripts/    build_windows.py（exe・ZIP・更新マニフェスト）、build_download.py（ソース ZIP）
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
- `dist/updates/windows.json` は **旧版 1.x だけ** が読む更新情報です。1.x の PC が残っている間は、Release 作成後に
  Release の `windows.json` をそのままコピーして push します（Vercel が自動で配信します）

  ```powershell
  gh release download v2.1.0 -p windows.json -O dist/updates/windows.json
  git commit -am "Point legacy update feed at 2.1.0" ; git push
  ```

## 安全性

- サーバーは `127.0.0.1` だけで待ち受け、iPhone からは Tailscale Serve（tailnet 内限定の HTTPS）経由で届きます
- 更新は公式 Releases の HTTPS URL だけを受け付け、ダウンロード後にサイズと SHA-256 を検証してから入れ替えます。配布版にコード署名はありません
- 接続トークンは各 PC の `%LOCALAPPDATA%\ClipBridge\config.json`（ソース版は `app/config.json`）に生成され、配布物には含まれません

## 注意

テキスト専用です。コピーの常時自動同期、画像やファイルの送受信には対応していません。利用時は PC の ClipBridge が動作し、両端末が同じ Tailscale ネットワークへ接続している必要があります。
