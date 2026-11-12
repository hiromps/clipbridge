"""ライセンスの保存先。本番は Neon（Postgres）、テストはメモリ。

どちらも同じメソッドを持つ。transaction() の中で例外が出たら、その中の書き込みはすべて取り消される。
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import secrets

LICENSE_FIELDS = (
    "id", "email", "name", "plan_interval", "stripe_customer_id", "stripe_subscription_id",
    "status", "current_period_end", "cancel_at_period_end", "revoked_at",
)


def new_license_id(now: dt.datetime) -> str:
    return f"CB-{now:%Y%m}-{secrets.token_hex(4).upper()}"


class PostgresStore:
    def __init__(self, connection) -> None:
        self.conn = connection

    @classmethod
    def connect(cls, url: str) -> "PostgresStore":
        import psycopg  # 本番（Vercel）だけで使う。テストは MemoryStore

        return cls(psycopg.connect(url, autocommit=True, connect_timeout=10))

    def close(self) -> None:
        self.conn.close()

    def transaction(self):
        return self.conn.transaction()

    def _one(self, sql: str, params: tuple) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(zip(LICENSE_FIELDS, row)) if row else None

    def record_event(self, event_id: str, event_type: str) -> bool:
        """初めて見るイベントなら True。"""
        cursor = self.conn.execute(
            "INSERT INTO stripe_events (id, type) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (event_id, event_type),
        )
        return cursor.rowcount == 1

    def get_license(self, license_id: str) -> dict | None:
        return self._one(f"SELECT {', '.join(LICENSE_FIELDS)} FROM licenses WHERE id = %s", (license_id,))

    def get_by_subscription(self, subscription_id: str) -> dict | None:
        return self._one(
            f"SELECT {', '.join(LICENSE_FIELDS)} FROM licenses WHERE stripe_subscription_id = %s", (subscription_id,),
        )

    def upsert_subscription(self, *, subscription_id: str, customer_id: str, email: str, name: str, status: str,
                            period_end: dt.datetime | None, cancel_at_period_end: bool, interval: str | None,
                            now: dt.datetime) -> dict:
        """契約（Stripe の subscription）に対応するライセンスを作る。すでにあれば状態だけ更新する。"""
        return self._one(
            f"""
            INSERT INTO licenses (id, email, name, plan_interval, stripe_customer_id, stripe_subscription_id,
                                  status, current_period_end, cancel_at_period_end)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stripe_subscription_id) DO UPDATE SET
                status = EXCLUDED.status,
                current_period_end = EXCLUDED.current_period_end,
                cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                plan_interval = EXCLUDED.plan_interval,
                updated_at = now()
            RETURNING {', '.join(LICENSE_FIELDS)}
            """,
            (new_license_id(now), email, name, interval, customer_id, subscription_id, status, period_end, cancel_at_period_end),
        )

    def update_subscription(self, *, subscription_id: str, status: str, period_end: dt.datetime | None,
                            cancel_at_period_end: bool, interval: str | None) -> dict | None:
        return self._one(
            f"""
            UPDATE licenses SET status = %s, current_period_end = %s, cancel_at_period_end = %s,
                                plan_interval = COALESCE(%s, plan_interval), updated_at = now()
            WHERE stripe_subscription_id = %s
            RETURNING {', '.join(LICENSE_FIELDS)}
            """,
            (status, period_end, cancel_at_period_end, interval, subscription_id),
        )

    def revoke_customer(self, customer_id: str, now: dt.datetime) -> int:
        cursor = self.conn.execute(
            "UPDATE licenses SET revoked_at = %s, updated_at = now() WHERE stripe_customer_id = %s AND revoked_at IS NULL",
            (now, customer_id),
        )
        return cursor.rowcount

    def touch_install(self, license_id: str, install_id: str, pc_name: str, version: str) -> None:
        self.conn.execute(
            """
            INSERT INTO license_installs (license_id, install_id, pc_name, version) VALUES (%s, %s, %s, %s)
            ON CONFLICT (license_id, install_id) DO UPDATE SET
                pc_name = EXCLUDED.pc_name, version = EXCLUDED.version, last_seen = now()
            """,
            (license_id, install_id, pc_name, version),
        )


class MemoryStore:
    """テスト用。PostgresStore と同じ振る舞いをメモリ上で行う。"""

    def __init__(self) -> None:
        self.licenses: dict[str, dict] = {}
        self.events: dict[str, str] = {}
        self.installs: dict[tuple[str, str], dict] = {}

    def close(self) -> None:
        pass

    @contextlib.contextmanager
    def transaction(self):
        snapshot = copy.deepcopy((self.licenses, self.events, self.installs))
        try:
            yield
        except BaseException:
            self.licenses, self.events, self.installs = snapshot
            raise

    def record_event(self, event_id: str, event_type: str) -> bool:
        if event_id in self.events:
            return False
        self.events[event_id] = event_type
        return True

    def get_license(self, license_id: str) -> dict | None:
        found = self.licenses.get(license_id)
        return dict(found) if found else None

    def get_by_subscription(self, subscription_id: str) -> dict | None:
        for row in self.licenses.values():
            if row["stripe_subscription_id"] == subscription_id:
                return dict(row)
        return None

    def upsert_subscription(self, *, subscription_id, customer_id, email, name, status, period_end,
                            cancel_at_period_end, interval, now) -> dict:
        existing = self.get_by_subscription(subscription_id)
        if existing:
            row = self.licenses[existing["id"]]
            row.update(status=status, current_period_end=period_end, cancel_at_period_end=cancel_at_period_end, plan_interval=interval)
            return dict(row)
        row = {
            "id": new_license_id(now), "email": email, "name": name, "plan_interval": interval,
            "stripe_customer_id": customer_id, "stripe_subscription_id": subscription_id, "status": status,
            "current_period_end": period_end, "cancel_at_period_end": cancel_at_period_end, "revoked_at": None,
        }
        self.licenses[row["id"]] = row
        return dict(row)

    def update_subscription(self, *, subscription_id, status, period_end, cancel_at_period_end, interval) -> dict | None:
        existing = self.get_by_subscription(subscription_id)
        if not existing:
            return None
        row = self.licenses[existing["id"]]
        row.update(status=status, current_period_end=period_end, cancel_at_period_end=cancel_at_period_end)
        if interval:
            row["plan_interval"] = interval
        return dict(row)

    def revoke_customer(self, customer_id: str, now) -> int:
        count = 0
        for row in self.licenses.values():
            if row["stripe_customer_id"] == customer_id and row["revoked_at"] is None:
                row["revoked_at"] = now
                count += 1
        return count

    def touch_install(self, license_id: str, install_id: str, pc_name: str, version: str) -> None:
        self.installs[(license_id, install_id)] = {"pc_name": pc_name, "version": version}
