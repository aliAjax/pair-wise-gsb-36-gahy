"""保存层：SQLite 表结构与全部 SQL 操作。

方法均接受由服务层开启事务后传入的连接；Database 自身只负责建库。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(__file__).resolve().parent.parent / "water_rights.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    region TEXT NOT NULL,
                    holder TEXT NOT NULL,
                    priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 5),
                    valid_from TEXT NOT NULL,
                    valid_to TEXT NOT NULL,
                    quota REAL NOT NULL CHECK(quota >= 0),
                    used REAL NOT NULL DEFAULT 0 CHECK(used >= 0),
                    created_at TEXT NOT NULL,
                    CHECK(valid_from <= valid_to)
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    to_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    amount REAL NOT NULL CHECK(amount > 0),
                    effective_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_by TEXT NOT NULL,
                    approved_by TEXT,
                    created_at TEXT NOT NULL,
                    approved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS usage_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    meter_event_id TEXT NOT NULL,
                    amount REAL NOT NULL CHECK(amount > 0),
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, meter_event_id)
                );
                CREATE TABLE IF NOT EXISTS season_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    region TEXT NOT NULL,
                    month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
                    max_fraction REAL NOT NULL CHECK(max_fraction > 0 AND max_fraction <= 1),
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(region, month)
                );
                CREATE TABLE IF NOT EXISTS impact_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_region TEXT NOT NULL,
                    target_region TEXT NOT NULL,
                    min_source_fraction REAL NOT NULL CHECK(min_source_fraction >= 0 AND min_source_fraction <= 1),
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(source_region, target_region)
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    amount REAL NOT NULL CHECK(amount > 0),
                    valid_from TEXT NOT NULL,
                    valid_to TEXT NOT NULL,
                    basis TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    occupied_amount REAL NOT NULL DEFAULT 0 CHECK(occupied_amount >= 0),
                    released_at TEXT,
                    release_date TEXT,
                    release_reason TEXT NOT NULL DEFAULT '',
                    released_by TEXT,
                    created_at TEXT NOT NULL,
                    CHECK(valid_from <= valid_to),
                    CHECK(occupied_amount <= amount)
                );
                CREATE TABLE IF NOT EXISTS reservation_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
                    transfer_id INTEGER NOT NULL REFERENCES transfers(id),
                    shortfall REAL NOT NULL CHECK(shortfall > 0),
                    status TEXT NOT NULL DEFAULT 'pending',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    UNIQUE(reservation_id, transfer_id)
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    # ---- 审计 ----
    def audit_insert(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
                     entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    # ---- 账户 ----
    def insert_account(self, conn: sqlite3.Connection, m: Any) -> int:
        cur = conn.execute(
            "INSERT INTO accounts(name,region,holder,priority,valid_from,valid_to,quota,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (m.name, m.region, m.holder, m.priority, m.valid_from.isoformat(),
             m.valid_to.isoformat(), m.quota, utcnow()),
        )
        return int(cur.lastrowid)

    def get_account(self, conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def require_account(self, conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
        row = self.get_account(conn, account_id)
        if not row:
            from .materials import DomainError
            raise DomainError("水权账户不存在", 404)
        return row

    def list_accounts(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()

    def move_quota(self, conn: sqlite3.Connection, source_id: int, target_id: int, amount: float) -> None:
        conn.execute("UPDATE accounts SET quota=quota-? WHERE id=?", (amount, source_id))
        conn.execute("UPDATE accounts SET quota=quota+? WHERE id=?", (amount, target_id))

    def add_used(self, conn: sqlite3.Connection, account_id: int, amount: float) -> None:
        conn.execute("UPDATE accounts SET used=used+? WHERE id=?", (amount, account_id))

    # ---- 规则 ----
    def upsert_season_rule(self, conn: sqlite3.Connection, region: str, month: int,
                           max_fraction: float, note: str) -> None:
        conn.execute(
            """INSERT INTO season_rules(region,month,max_fraction,note) VALUES(?,?,?,?)
               ON CONFLICT(region,month) DO UPDATE SET max_fraction=excluded.max_fraction,note=excluded.note""",
            (region.strip(), int(month), float(max_fraction), note),
        )

    def upsert_impact_rule(self, conn: sqlite3.Connection, source_region: str, target_region: str,
                           min_source_fraction: float, note: str) -> None:
        conn.execute(
            """INSERT INTO impact_rules(source_region,target_region,min_source_fraction,note) VALUES(?,?,?,?)
               ON CONFLICT(source_region,target_region) DO UPDATE
               SET min_source_fraction=excluded.min_source_fraction,note=excluded.note""",
            (source_region, target_region, float(min_source_fraction), note),
        )

    def get_season_rule(self, conn: sqlite3.Connection, region: str, month: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT max_fraction FROM season_rules WHERE region=? AND month=?", (region, month)
        ).fetchone()

    def get_impact_rule(self, conn: sqlite3.Connection, source_region: str,
                        target_region: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?",
            (source_region, target_region),
        ).fetchone()

    # ---- 转让 ----
    def insert_transfer(self, conn: sqlite3.Connection, m: Any, actor: str) -> int:
        cur = conn.execute(
            "INSERT INTO transfers(from_account_id,to_account_id,amount,effective_date,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (m.from_account_id, m.to_account_id, m.amount, m.effective_date.isoformat(), actor, utcnow()),
        )
        return int(cur.lastrowid)

    def get_transfer(self, conn: sqlite3.Connection, transfer_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()

    def list_transfers(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM transfers ORDER BY id DESC").fetchall()

    def pending_outgoing(self, conn: sqlite3.Connection, account_id: int,
                         exclude_id: int | None = None) -> float:
        if exclude_id is None:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE from_account_id=? AND status='pending'",
                (account_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE from_account_id=? AND status='pending' AND id<>?",
                (account_id, exclude_id),
            ).fetchone()
        return float(row["total"])

    def pending_outgoing_from(self, conn: sqlite3.Connection, account_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM transfers WHERE from_account_id=? AND status='pending' ORDER BY id",
            (account_id,),
        ).fetchall()

    def mark_transfer(self, conn: sqlite3.Connection, transfer_id: int, status: str, actor: str) -> None:
        conn.execute(
            "UPDATE transfers SET status=?,approved_by=?,approved_at=? WHERE id=?",
            (status, actor, utcnow(), transfer_id),
        )

    # ---- 取水 ----
    def insert_usage(self, conn: sqlite3.Connection, m: Any, actor: str) -> int:
        cur = conn.execute(
            "INSERT INTO usage_records(account_id,meter_event_id,amount,occurred_at,actor,created_at) VALUES(?,?,?,?,?,?)",
            (m.account_id, m.meter_event_id, m.amount, m.occurred_at.isoformat(), actor, utcnow()),
        )
        return int(cur.lastrowid)

    def get_usage(self, conn: sqlite3.Connection, usage_id: int) -> sqlite3.Row:
        return conn.execute("SELECT * FROM usage_records WHERE id=?", (usage_id,)).fetchone()

    def month_usage(self, conn: sqlite3.Connection, account_id: int, month_key: str) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) total FROM usage_records WHERE account_id=? AND substr(occurred_at,1,7)=?",
            (account_id, month_key),
        ).fetchone()
        return float(row["total"])

    # ---- 生态预留 ----
    def insert_reservation(self, conn: sqlite3.Connection, m: Any) -> int:
        cur = conn.execute(
            """INSERT INTO reservations(account_id,amount,valid_from,valid_to,basis,operator,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (m.account_id, m.amount, m.valid_from.isoformat(), m.valid_to.isoformat(),
             m.basis, m.operator, utcnow()),
        )
        return int(cur.lastrowid)

    def get_reservation(self, conn: sqlite3.Connection, reservation_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()

    def list_reservations(self, conn: sqlite3.Connection, account_id: int | None = None) -> list[sqlite3.Row]:
        if account_id is None:
            return conn.execute("SELECT * FROM reservations ORDER BY id DESC").fetchall()
        return conn.execute(
            "SELECT * FROM reservations WHERE account_id=? ORDER BY id DESC", (account_id,)
        ).fetchall()

    def update_reservation_release(self, conn: sqlite3.Connection, reservation_id: int,
                                   release_date: str, reason: str, actor: str) -> None:
        conn.execute(
            """UPDATE reservations SET status='released',release_date=?,released_at=?,release_reason=?,released_by=?
               WHERE id=?""",
            (release_date, utcnow(), reason, actor, reservation_id),
        )

    def add_occupied(self, conn: sqlite3.Connection, reservation_id: int, amount: float) -> None:
        conn.execute(
            "UPDATE reservations SET occupied_amount=occupied_amount+? WHERE id=?",
            (amount, reservation_id),
        )

    # ---- 预留冲突（待处理区） ----
    def insert_conflict(self, conn: sqlite3.Connection, reservation_id: int,
                        transfer_id: int, shortfall: float, note: str) -> int:
        cur = conn.execute(
            """INSERT INTO reservation_conflicts(reservation_id,transfer_id,shortfall,note,created_at)
               VALUES(?,?,?,?,?)""",
            (reservation_id, transfer_id, shortfall, note, utcnow()),
        )
        return int(cur.lastrowid)

    def list_conflicts(self, conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT c.*, r.account_id AS reservation_account_id, r.amount AS reservation_amount, "
            "r.valid_from AS reservation_from, r.valid_to AS reservation_to, r.basis AS reservation_basis, "
            "t.from_account_id, t.to_account_id, t.amount AS transfer_amount, "
            "t.effective_date, t.status AS transfer_status, t.created_by "
            "FROM reservation_conflicts c "
            "JOIN reservations r ON r.id=c.reservation_id "
            "JOIN transfers t ON t.id=c.transfer_id "
        )
        if status:
            sql += "WHERE c.status=? ORDER BY c.id"
            return conn.execute(sql, (status,)).fetchall()
        sql += "ORDER BY c.id"
        return conn.execute(sql).fetchall()

    def pending_conflicts_for_transfer(self, conn: sqlite3.Connection, transfer_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM reservation_conflicts WHERE transfer_id=? AND status='pending' ORDER BY id",
            (transfer_id,),
        ).fetchall()

    def resolve_conflicts_for_transfer(self, conn: sqlite3.Connection, transfer_id: int,
                                       status: str, note: str) -> None:
        conn.execute(
            "UPDATE reservation_conflicts SET status=?,note=?,resolved_at=? WHERE transfer_id=? AND status='pending'",
            (status, note, utcnow(), transfer_id),
        )

    def resolve_conflict(self, conn: sqlite3.Connection, conflict_id: int, status: str, note: str) -> int:
        cur = conn.execute(
            "UPDATE reservation_conflicts SET status=?,note=?,resolved_at=? WHERE id=? AND status='pending'",
            (status, note, utcnow(), conflict_id),
        )
        return cur.rowcount

    def get_conflict(self, conn: sqlite3.Connection, conflict_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM reservation_conflicts WHERE id=?", (conflict_id,)).fetchone()

    # ---- 审计查询 ----
    def list_audit(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()
