"""ClipBridge Pro の発行サーバー（Vercel Functions から使う共通コード）。

    keys.py     ライセンスキーの署名と検証（アプリ app/server.py と同じ形式）
    stripe.py   Stripe の REST 呼び出しと webhook の署名検証（標準ライブラリのみ）
    store.py    Neon（Postgres）への読み書き
    service.py  購入・webhook・キーの受け取り・更新・契約管理のロジック
    web.py      HTTP ハンドラ（api/ 配下の各ファイルが 1 つずつ公開する）
"""
