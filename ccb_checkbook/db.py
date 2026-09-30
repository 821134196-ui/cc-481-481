# -*- coding: utf-8 -*-
"""SQLite 持久化：网点 / 客户 / 支票簿 / 单张号码状态 / 销毁批次。

设计要点
--------
* ``check_number`` 表对 (branch_code, check_no) 建唯一索引 ——
  同一号码在全行范围内只能处于一个有效状态，预留即落库，重复预留由
  数据库约束兜底（IntegrityError → BusinessError）。
* 每个业务动作写一行 ``status_log``，审计链可回放。
* 连接默认 ``isolation_level=None``（autocommit），由服务层显式
  BEGIN IMMEDIATE，保证“两个柜员抢占同一支票簿时仅首笔成功”。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

# ---- 支票簿（一本 = 一个连续号码段）状态 ----
BK_DRAFT = "DRAFT"              # 号码段已预留，申请审核中（占位行）
BK_RESERVED = "RESERVED"        # 申请通过，号码段已预留，待出库
BK_RELEASED = "RELEASED"        # 双人出库完成，待客户签收
BK_ACTIVE = "ACTIVE"            # 客户签收，正式启用
BK_STOP_PAY = "STOP_PAY"        # 挂失止付（未使用号码冻结）
BK_PART_VOID = "PART_VOID"      # 销户交回，部分空白页作废，余号仍止付
BK_FULLY_VOID = "FULLY_VOID"    # 所有空白页均已作废打孔，待销毁
BK_DESTROYED = "DESTROYED"      # 整本销毁入账
BK_REJECTED = "REJECTED"        # 审核驳回 / 客户拒领（占位行释放）

BOOK_STATUSES = {
    BK_DRAFT, BK_RESERVED, BK_RELEASED, BK_ACTIVE, BK_STOP_PAY,
    BK_PART_VOID, BK_FULLY_VOID, BK_DESTROYED, BK_REJECTED,
}

# ---- 单张支票号码状态 ----
ST_RESERVED = "RESERVED"        # 已预留（不可再分配）
ST_RELEASED = "RELEASED"        # 已随簿出库
ST_ACTIVE = "ACTIVE"            # 已签收，客户可签发
ST_USED = "USED"                # 客户已使用（终态，不参与交回/销毁）
ST_STOP = "STOP"                # 挂失止付冻结
ST_VOID = "VOID"                # 空白页交回，逐张作废打孔
ST_DESTROYED = "DESTROYED"      # 已进入销毁批次（终态，不可逆）
ST_REJECTED = "REJECTED"        # 申请驳回/拒领后释放的占位号（终态占用，不再复用）

NUMBER_STATUSES = {
    ST_RESERVED, ST_RELEASED, ST_ACTIVE, ST_USED, ST_STOP,
    ST_VOID, ST_DESTROYED, ST_REJECTED,
}
# 可随“空白页交回”作废的状态
VOIDABLE = {ST_RESERVED, ST_RELEASED, ST_ACTIVE, ST_STOP}
# 可进入销毁批次的状态
DESTROYABLE = {ST_VOID}
# 终止性状态
TERMINAL_NUMBER = {ST_USED, ST_DESTROYED, ST_REJECTED}

# ---- 销毁批次状态 ----
DEST_OPEN = "OPEN"              # 建批，可继续归集
DEST_CONFIRMED = "CONFIRMED"    # 双人确认封存
DEST_DESTROYED = "DESTROYED"    # 已销毁（差异登记），台账封存


class BusinessError(Exception):
    """业务规则违反（HTTP 层统一转 400 页面）。"""


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS branches (
    code        TEXT PRIMARY KEY,          -- 网点代号，如 BJ001
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS customers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_code TEXT NOT NULL REFERENCES branches(code),
    cust_no     TEXT NOT NULL,              -- 客户代号（虚构）
    acct_no     TEXT NOT NULL,              -- 账户代号（虚构）
    cust_name   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'NORMAL', -- NORMAL / CLOSED
    UNIQUE(branch_code, acct_no)
);

CREATE TABLE IF NOT EXISTS stock_ranges (
    -- 网点号码库存区间（虚构），用于预留时找连续段与识别缺页
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_code TEXT NOT NULL REFERENCES branches(code),
    start_no    INTEGER NOT NULL,
    end_no      INTEGER NOT NULL,
    CHECK (end_no >= start_no)
);

CREATE TABLE IF NOT EXISTS checkbooks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_code   TEXT NOT NULL REFERENCES branches(code),
    acct_no       TEXT NOT NULL,
    cust_no       TEXT NOT NULL,
    cust_name     TEXT NOT NULL,
    start_no      INTEGER NOT NULL,
    end_no        INTEGER NOT NULL,
    status        TEXT NOT NULL,
    apply_ref     TEXT NOT NULL UNIQUE,     -- 申请编号
    teller_id     TEXT,                     -- 经办柜员账号
    reviewer_id   TEXT,                     -- 复核柜员账号
    release_teller   TEXT,
    release_reviewer TEXT,
    released_at   TEXT,
    signed_at     TEXT,
    stop_at       TEXT,
    stop_reason   TEXT,
    refused_at    TEXT,
    refuse_reason TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK (end_no >= start_no),
    CHECK (release_teller IS NULL OR release_teller <> release_reviewer),
    CHECK (teller_id  IS NULL OR teller_id  <> reviewer_id)
);

CREATE TABLE IF NOT EXISTS check_numbers (
    -- 一行 = 一个实物号码。全行同号唯一。
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_code TEXT NOT NULL REFERENCES branches(code),
    check_no    INTEGER NOT NULL,
    book_id     INTEGER REFERENCES checkbooks(id),
    status      TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(branch_code, check_no)
);
CREATE INDEX IF NOT EXISTS idx_num_book   ON check_numbers(book_id);
CREATE INDEX IF NOT EXISTS idx_num_status ON check_numbers(branch_code, status);

CREATE TABLE IF NOT EXISTS handovers (
    -- 空白页交回单（销户清点）
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id         INTEGER NOT NULL REFERENCES checkbooks(id),
    expected_count  INTEGER NOT NULL,       -- 系统计算应交回空白数
    returned_count  INTEGER NOT NULL,       -- 实物清点张数
    missing_count   INTEGER NOT NULL,       -- 缺口（未随簿的号）
    status          TEXT NOT NULL,          -- MATCH / MISMATCH
    teller_id       TEXT NOT NULL,
    reviewer_id     TEXT NOT NULL,
    voucher_path    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK (teller_id <> reviewer_id)
);

CREATE TABLE IF NOT EXISTS void_actions (
    -- 逐张作废打孔记录
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES checkbooks(id),
    check_no    INTEGER NOT NULL,
    handover_id INTEGER REFERENCES handovers(id),
    teller_id   TEXT NOT NULL,
    voucher_path TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS destroy_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_code  TEXT NOT NULL REFERENCES branches(code),
    batch_no     TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL,             -- OPEN / CONFIRMED / DESTROYED
    confirm_a    TEXT,                      -- 确认人甲
    confirm_b    TEXT,                      -- 确认人乙（必须不同）
    confirmed_at TEXT,
    destroyed_at TEXT,
    expect_count INTEGER NOT NULL DEFAULT 0,
    actual_count INTEGER,                   -- 实物销毁点数
    diff_count   INTEGER,                   -- 销毁差异
    diff_note    TEXT,
    voucher_path TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK (confirm_a IS NULL OR confirm_a <> confirm_b)
);

CREATE TABLE IF NOT EXISTS status_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity      TEXT NOT NULL,              -- BOOK / NUMBER / HANDOVER / BATCH
    entity_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT,
    operator    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,              -- PENDING_PICKUP / PENDING_DESTROY
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

# destroy_items 需要带 branch_code 才能做号码级唯一，这里单独补建。
DESTROY_ITEMS_FIX = """
CREATE TABLE IF NOT EXISTS destroy_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES destroy_batches(id),
    branch_code TEXT NOT NULL,
    book_id     INTEGER NOT NULL REFERENCES checkbooks(id),
    check_no    INTEGER NOT NULL,
    added_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(branch_code, check_no)
);
CREATE INDEX IF NOT EXISTS idx_ditem_batch ON destroy_items(batch_id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        # destroy_items 必须携带 branch_code 做号码级唯一，单独建表。
        conn.executescript(DESTROY_ITEMS_FIX)
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    """显式立即事务：写操作互斥，抢占场景下后来者直接拿锁失败。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def log(conn: sqlite3.Connection, entity: str, entity_id, action: str,
        detail: str = "", operator: str = "") -> None:
    conn.execute(
        "INSERT INTO status_log(entity, entity_id, action, detail, operator)"
        " VALUES (?,?,?,?,?)",
        (entity, str(entity_id), action, detail, operator),
    )
