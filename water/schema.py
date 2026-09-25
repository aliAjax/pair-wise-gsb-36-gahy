"""资料层：全部表结构定义。"""
from __future__ import annotations

SCHEMA_SQL = """
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
-- 生态预留：pending（已确认待生效）/ active（生效中）/ released（提前解除）。
-- released 记录在 [start_date, 解除日] 内仍按原金额锁定额度，restored_amount
-- 仅登记解除时仍可恢复的部分，余额本身不划转，随窗口结束自动释放。
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount > 0),
    basis TEXT NOT NULL,
    operator TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    restored_amount REAL NOT NULL DEFAULT 0 CHECK(restored_amount >= 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_by TEXT,
    activated_at TEXT,
    released_by TEXT,
    released_at TEXT,
    CHECK(start_date <= end_date)
);
-- 待处理区：预留生效后放不下、被挤到的待审转让。转让被批准/退回或额度松动
-- 后由保存层统一刷新状态，冲突全程留痕。
CREATE TABLE IF NOT EXISTS reservation_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    transfer_id INTEGER NOT NULL REFERENCES transfers(id),
    shortage REAL NOT NULL CHECK(shortage >= 0),
    status TEXT NOT NULL DEFAULT 'open',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
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
