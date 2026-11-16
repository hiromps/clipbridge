-- 購入完了メール（ライセンスキーの案内）を送った時刻。二重送信を防ぐ。冪等に書く。
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS key_emailed_at timestamptz;
