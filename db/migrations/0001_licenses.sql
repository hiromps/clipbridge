-- ClipBridge Pro のサブスクリプション管理（発行サーバー用）。冪等に書く。

-- 1 契約 = 1 行。id はライセンスキーに入る「ライセンス ID」
CREATE TABLE IF NOT EXISTS licenses (
    id                      text PRIMARY KEY,
    email                   text NOT NULL,
    name                    text NOT NULL DEFAULT '',
    plan_interval           text,                          -- month / year
    stripe_customer_id      text NOT NULL,
    stripe_subscription_id  text NOT NULL UNIQUE,
    status                  text NOT NULL,                 -- Stripe の subscription.status をそのまま持つ
    current_period_end      timestamptz,
    cancel_at_period_end    boolean NOT NULL DEFAULT false,
    revoked_at              timestamptz,                   -- 返金などで取り消した時刻（入っていれば即時に無効）
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS licenses_customer_idx ON licenses (stripe_customer_id);

-- 更新を問い合わせてきた PC（マイページの一覧と、将来の台数制限の土台）
CREATE TABLE IF NOT EXISTS license_installs (
    license_id  text NOT NULL REFERENCES licenses (id) ON DELETE CASCADE,
    install_id  text NOT NULL,
    pc_name     text NOT NULL DEFAULT '',
    version     text NOT NULL DEFAULT '',
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (license_id, install_id)
);

-- 処理済みの Stripe イベント（webhook の二重処理を防ぐ）
CREATE TABLE IF NOT EXISTS stripe_events (
    id           text PRIMARY KEY,
    type         text NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now()
);
