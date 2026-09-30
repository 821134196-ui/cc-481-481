"""SQLite 数据访问层：建表、连接与号码状态机守卫。

核心约束：
- check_pages(branch_code, check_no) 为唯一键，且 check_no 在网点内唯一，
  因此同一号码不可能同时属于两个有效状态。
- 所有状态迁移在短事务内完成，两个柜员抢占同一支票簿时仅首笔成功。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS branches (
    code        TEXT PRIMARY KEY,        -- 登记网点代号
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,        -- 客户代号
    name        TEXT NOT NULL,
    id_tail     TEXT NOT NULL            -- 虚构证件后四位
);

CREATE TABLE IF NOT EXISTS accounts (
    account_no  TEXT PRIMARY KEY,        -- 账户代号
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    branch_code TEXT NOT NULL REFERENCES branches(code),
    status      TEXT NOT NULL DEFAULT 'normal'  -- normal / closed
);

CREATE TABLE IF NOT EXISTS staff (
    account     TEXT PRIMARY KEY,        -- 员工账号（经办/复核分离的判定对象）
    name        TEXT NOT NULL,
    role        TEXT NOT NULL            -- teller / reviewer / supervisor
);

CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_code     TEXT NOT NULL REFERENCES branches(code),
    account_no      TEXT NOT NULL REFERENCES accounts(account_no),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
    qualifier       TEXT,                              -- 资格核验结论
    reviewer        TEXT REFERENCES staff(account),
    created_at      TEXT NOT NULL,
    decided_at      TEXT
);

CREATE TABLE IF NOT EXISTS booklets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_code     TEXT NOT NULL REFERENCES branches(code),
    account_no      TEXT NOT NULL REFERENCES accounts(account_no),
    application_id  INTEGER REFERENCES applications(id),
    start_no        INTEGER NOT NULL,
    end_no          INTEGER NOT NULL CHECK (end_no >= start_no),
    status          TEXT NOT NULL DEFAULT 'reserved',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(branch_code, start_no, end_no)
);

-- 支票页（号码）台账：一行一个号码，状态即唯一有效状态
CREATE TABLE IF NOT EXISTS check_pages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_code     TEXT NOT NULL REFERENCES branches(code),
    check_no        INTEGER NOT NULL,
    booklet_id      INTEGER REFERENCES booklets(id),
    status          TEXT NOT NULL DEFAULT 'available',
    updated_at      TEXT NOT NULL,
    UNIQUE(branch_code, check_no)        -- 号码唯一：同一号码仅一条状态
);

-- 号段缺口登记（库存中物理缺页的号码）
CREATE TABLE IF NOT EXISTS stock_gaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_code TEXT NOT NULL REFERENCES branches(code),
    check_no    INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(branch_code, check_no)
);

CREATE TABLE IF NOT EXISTS disbursals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    booklet_id  INTEGER NOT NULL REFERENCES booklets(id),
    handler     TEXT NOT NULL REFERENCES staff(account),   -- 经办柜员
    reviewer    TEXT NOT NULL REFERENCES staff(account),   -- 复核柜员
    physical_qty INTEGER NOT NULL,                         -- 实物数量
    created_at  TEXT NOT NULL,
    CHECK (handler <> reviewer)                            -- 经办复核必须分离
);

CREATE TABLE IF NOT EXISTS signoffs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    booklet_id  INTEGER NOT NULL REFERENCES booklets(id),
    result      TEXT NOT NULL,      -- signed / failed / refused
    signature   TEXT,
    note        TEXT,
    voucher_path TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stop_orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    booklet_id  INTEGER NOT NULL REFERENCES booklets(id),
    reason      TEXT NOT NULL,
    frozen_qty  INTEGER NOT NULL DEFAULT 0,
    late        INTEGER NOT NULL DEFAULT 0,   -- 止付迟到：到达时号码已作废/销毁
    status      TEXT NOT NULL DEFAULT 'active', -- active / lifted
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS returns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    booklet_id      INTEGER NOT NULL REFERENCES booklets(id),
    expected_qty    INTEGER NOT NULL,  -- 应空白数（未使用号码）
    actual_qty      INTEGER NOT NULL,  -- 实交数
    mismatch        INTEGER NOT NULL DEFAULT 0,
    handler         TEXT NOT NULL REFERENCES staff(account),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voids (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_code TEXT NOT NULL REFERENCES branches(code),
    check_no    INTEGER NOT NULL,
    booklet_id  INTEGER REFERENCES booklets(id),
    reason      TEXT NOT NULL,         -- refused_return / account_close / punched
    operator    TEXT NOT NULL REFERENCES staff(account),
    voucher_path TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(branch_code, check_no)
);

CREATE TABLE IF NOT EXISTS destroy_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_code     TEXT NOT NULL REFERENCES branches(code),
    status          TEXT NOT NULL DEFAULT 'open',  -- open/discrepancy/confirmed
    expected_qty    INTEGER NOT NULL DEFAULT 0,
    actual_qty      INTEGER,
    confirmer_a     TEXT REFERENCES staff(account),
    confirmer_b     TEXT REFERENCES staff(account),
    created_at      TEXT NOT NULL,
    confirmed_at    TEXT,
    CHECK (confirmer_a IS NULL OR confirmer_b IS NULL OR confirmer_a <> confirmer_b)
);

CREATE TABLE IF NOT EXISTS destroy_items (
    batch_id    INTEGER NOT NULL REFERENCES destroy_batches(id),
    branch_code TEXT NOT NULL,
    check_no    INTEGER NOT NULL,
    destroyed   INTEGER NOT NULL DEFAULT 0,  -- 双人确认后置 1
    PRIMARY KEY (branch_code, check_no)
);

CREATE TABLE IF NOT EXISTS vouchers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,      -- signoff / void / destroy
    ref_no      TEXT NOT NULL,      -- 业务编号
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,      -- apply/inventory/disburse/signoff/stop/return/destroy/scan
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pages_status ON check_pages(branch_code, status);
CREATE INDEX IF NOT EXISTS idx_pages_booklet ON check_pages(booklet_id);
"""


def connect(db_path=None) -> sqlite3.Connection:
    if db_path is None:
        db_path = config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config.VOUCHER_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务：配合 BEGIN IMMEDIATE 保证抢占时仅首笔成功。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
