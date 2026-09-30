"""领域服务：支票簿从申请到销毁的全流程状态机。

流程主线：
  申请 pending ──审核+资格适配器──▶ approved ──连续号段预留──▶ reserved
  reserved ──双人出库（经办≠复核，实物数量一致）──▶ issued
  issued ──签收适配器──▶ signed: active / failed: claim_failed / refused: refused+逐张作废
  active/issued ──挂失止付（冻结未使用号码，迟到止付单独标记）──▶ stopped
  销户 ──清点空白页（数量不符单独标记）──▶ 逐张作废打孔 void
  void ──进入销毁批次，双人确认（差异单独标记）──▶ destroyed（终态不可逆）

所有迁移均在 BEGIN IMMEDIATE 事务内完成；号码唯一由
UNIQUE(branch_code, check_no) 与“条件更新 rowcount”双重保证。
"""
from __future__ import annotations

from datetime import datetime

from . import adapters, config, vouchers
from .database import tx


class ServiceError(Exception):
    """业务校验失败（对用户显示的中文消息）。"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_event(conn, category: str, message: str) -> None:
    conn.execute(
        "INSERT INTO event_log(category, message, created_at) VALUES (?,?,?)",
        (category, message, now()),
    )


# ---------------------------------------------------------------- 申请与审核

def submit_application(conn, branch_code: str, account_no: str, quantity: int) -> int:
    if quantity <= 0:
        raise ServiceError("申请数量必须大于 0")
    with tx(conn):
        row = conn.execute("SELECT 1 FROM accounts WHERE account_no=?", (account_no,)).fetchone()
        if row is None:
            raise ServiceError("账户不存在（虚构数据）")
        cur = conn.execute(
            "INSERT INTO applications(branch_code, account_no, quantity, status, created_at) "
            "VALUES (?,?,?,?,?)",
            (branch_code, account_no, quantity, config.APP_PENDING, now()),
        )
        app_id = cur.lastrowid
        log_event(conn, "apply", f"申请 #{app_id}：账户 {account_no} 申请支票 {quantity} 张，待审核")
        return app_id


def review_application(conn, application_id: int, reviewer: str, approve: bool) -> dict:
    """审核申请；通过则调用资格适配器并立即预留连续号段。"""
    with tx(conn):
        app = conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
        if app is None:
            raise ServiceError("申请不存在")
        if app["status"] != config.APP_PENDING:
            raise ServiceError(f"申请 #{application_id} 已审核，不可重复审核")
        if not approve:
            conn.execute(
                "UPDATE applications SET status=?, reviewer=?, decided_at=? WHERE id=?",
                (config.APP_REJECTED, reviewer, now(), application_id),
            )
            log_event(conn, "apply", f"申请 #{application_id} 审核不通过（复核 {reviewer}）")
            return {"application_id": application_id, "status": config.APP_REJECTED}

        ok, detail = adapters.qualify_account(conn, app["account_no"])
        if not ok:
            conn.execute(
                "UPDATE applications SET status=?, qualifier=?, reviewer=?, decided_at=? WHERE id=?",
                (config.APP_REJECTED, detail, reviewer, now(), application_id),
            )
            log_event(conn, "apply", f"申请 #{application_id} 资格核验不通过：{detail}")
            return {"application_id": application_id, "status": config.APP_REJECTED, "qualifier": detail}

        seg = _reserve_segment(conn, app["branch_code"], app["quantity"])
        cur = conn.execute(
            "INSERT INTO booklets(branch_code, account_no, application_id, start_no, end_no, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (app["branch_code"], app["account_no"], application_id, seg[0], seg[-1],
             config.BK_RESERVED, now(), now()),
        )
        booklet_id = cur.lastrowid
        conn.executemany(
            "UPDATE check_pages SET status=?, booklet_id=?, updated_at=? "
            "WHERE branch_code=? AND check_no=? AND status='available'",
            [(config.PAGE_RESERVED, booklet_id, now(), app["branch_code"], n) for n in seg],
        )
        conn.execute(
            "UPDATE applications SET status=?, qualifier=?, reviewer=?, decided_at=? WHERE id=?",
            (config.APP_APPROVED, detail, reviewer, now(), application_id),
        )
        log_event(
            conn, "apply",
            f"申请 #{application_id} 通过：{detail}；预留连续号段 {seg[0]:08d}-{seg[-1]:08d}（{len(seg)} 张）",
        )
        return {"application_id": application_id, "booklet_id": booklet_id,
                "start_no": seg[0], "end_no": seg[-1], "status": config.APP_APPROVED}


def _reserve_segment(conn, branch_code: str, quantity: int) -> list[int]:
    """在库存中寻找长度为 quantity 的连续在库号码段；缺页号码会打断连续性。

    重复预留由本事务内对 available 状态的条件更新杜绝：
    两个柜员并发审核时，BEGIN IMMEDIATE 串行化，后到者看到的已不是 available。
    """
    rows = conn.execute(
        "SELECT check_no FROM check_pages WHERE branch_code=? AND status='available' ORDER BY check_no",
        (branch_code,),
    ).fetchall()
    free = [r["check_no"] for r in rows]
    if len(free) < quantity:
        raise ServiceError(
            f"可用空白支票仅 {len(free)} 张，无法满足 {quantity} 张申请（号段库存不足）"
        )
    run: list[int] = []
    for n in free:
        if run and n != run[-1] + 1:
            run = []  # 遇到缺口（含物理缺页），重新起段
        run.append(n)
        if len(run) == quantity:
            return run
    gaps = conn.execute(
        "SELECT check_no FROM stock_gaps WHERE branch_code=? ORDER BY check_no", (branch_code,)
    ).fetchall()
    if gaps:
        gap_hint = "；库存登记缺页号码：" + ", ".join(f"{g['check_no']:08d}" for g in gaps)
    else:
        gap_hint = ""
    raise ServiceError(f"库存中不存在 {quantity} 张连续号码（号码缺页阻断连续号段）{gap_hint}")


# ---------------------------------------------------------------- 双人出库

def disburse(conn, booklet_id: int, handler: str, reviewer: str, physical_qty: int) -> int:
    if handler == reviewer:
        raise ServiceError("经办与复核必须为不同员工账号，双人出库被拒绝")
    _staff_exists(conn, handler)
    _staff_exists(conn, reviewer)
    with tx(conn):
        # 条件更新：仅首笔能把 reserved 改为 issued，并发抢占时第二笔 rowcount=0
        cur = conn.execute(
            "UPDATE booklets SET status=?, updated_at=? WHERE id=? AND status=?",
            (config.BK_ISSUED, now(), booklet_id, config.BK_RESERVED),
        )
        if cur.rowcount == 0:
            row = conn.execute("SELECT status FROM booklets WHERE id=?", (booklet_id,)).fetchone()
            state = config.BOOKLET_STATUS_LABEL.get(row["status"], row["status"]) if row else "不存在"
            raise ServiceError(f"支票簿 #{booklet_id} 当前为「{state}」，抢占失败：仅首笔出库成功")
        bk = conn.execute("SELECT * FROM booklets WHERE id=?", (booklet_id,)).fetchone()
        expected = bk["end_no"] - bk["start_no"] + 1
        if physical_qty != expected:
            # 实物数量不符：回滚状态迁移
            raise ServiceError(f"实物数量 {physical_qty} 与号段数量 {expected} 不一致，禁止出库")
        conn.execute(
            "UPDATE check_pages SET status=?, updated_at=? WHERE booklet_id=? AND status=?",
            (config.PAGE_ISSUED, now(), booklet_id, config.PAGE_RESERVED),
        )
        cur = conn.execute(
            "INSERT INTO disbursals(booklet_id, handler, reviewer, physical_qty, created_at) "
            "VALUES (?,?,?,?,?)",
            (booklet_id, handler, reviewer, physical_qty, now()),
        )
        log_event(
            conn, "disburse",
            f"支票簿 #{booklet_id} 双人出库：经办 {handler}、复核 {reviewer}，实物 {physical_qty} 张",
        )
        return cur.lastrowid


def _staff_exists(conn, account: str) -> None:
    if conn.execute("SELECT 1 FROM staff WHERE account=?", (account,)).fetchone() is None:
        raise ServiceError(f"员工账号 {account} 不存在（虚构数据）")


# ---------------------------------------------------------------- 签收与启用

def customer_signoff(conn, booklet_id: int) -> dict:
    """调用模拟签名适配器。签收成功才启用；失败留待重签；拒领则逐张作废。"""
    with tx(conn):
        bk = conn.execute("SELECT * FROM booklets WHERE id=?", (booklet_id,)).fetchone()
        if bk is None:
            raise ServiceError("支票簿不存在")
        if bk["status"] == config.BK_ACTIVE:
            raise ServiceError(f"支票簿 #{booklet_id} 已签收启用，禁止重复签收")
        if bk["status"] != config.BK_ISSUED and bk["status"] != config.BK_CLAIM_FAILED:
            raise ServiceError(
                f"支票簿 #{booklet_id} 当前为「{config.BOOKLET_STATUS_LABEL[bk['status']]}」，不可签收"
            )
        result, note = adapters.request_signature(conn, bk["account_no"])
        cust = conn.execute(
            "SELECT name FROM customers WHERE customer_id=(SELECT customer_id FROM accounts WHERE account_no=?)",
            (bk["account_no"],),
        ).fetchone()["name"]
        qty = bk["end_no"] - bk["start_no"] + 1

        if result == "signed":
            conn.execute(
                "UPDATE booklets SET status=?, updated_at=? WHERE id=?",
                (config.BK_ACTIVE, now(), booklet_id),
            )
            conn.execute(
                "UPDATE check_pages SET status=?, updated_at=? WHERE booklet_id=? AND status=?",
                (config.PAGE_ACTIVE, now(), booklet_id, config.PAGE_ISSUED),
            )
            vp = vouchers.signoff_voucher(conn, booklet_id, bk["branch_code"], bk["start_no"], bk["end_no"], qty, cust, "signed", note)
            conn.execute(
                "INSERT INTO signoffs(booklet_id, result, signature, note, voucher_path, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (booklet_id, "signed", f"{cust}（虚构签名）", note, vp, now()),
            )
            log_event(conn, "signoff", f"支票簿 #{booklet_id} 签收成功并启用：{note}，凭证 {vp}")
            return {"result": "signed", "note": note, "voucher": vp}

        if result == "failed":
            conn.execute(
                "UPDATE booklets SET status=?, updated_at=? WHERE id=?",
                (config.BK_CLAIM_FAILED, now(), booklet_id),
            )
            conn.execute(
                "INSERT INTO signoffs(booklet_id, result, note, created_at) VALUES (?,?,?,?)",
                (booklet_id, "failed", note, now()),
            )
            log_event(conn, "signoff", f"支票簿 #{booklet_id} 签收失败（{note}），保留待客户重新签收")
            return {"result": "failed", "note": note}

        # refused：客户拒领，未签收不得启用，全部空白页逐张作废
        conn.execute(
            "UPDATE booklets SET status=?, updated_at=? WHERE id=?",
            (config.BK_REFUSED, now(), booklet_id),
        )
        conn.execute(
            "INSERT INTO signoffs(booklet_id, result, note, created_at) VALUES (?,?,?,?)",
            (booklet_id, "refused", note, now()),
        )
        pages = conn.execute(
            "SELECT check_no FROM check_pages WHERE booklet_id=? ORDER BY check_no", (booklet_id,)
        ).fetchall()
        operator = conn.execute("SELECT account FROM staff WHERE role='teller' ORDER BY account LIMIT 1").fetchone()
        op = operator["account"] if operator else "T001"
        for p in pages:
            _void_page_locked(conn, bk["branch_code"], p["check_no"], booklet_id, "客户拒领交回，当场打孔作废", op)
        log_event(conn, "signoff", f"支票簿 #{booklet_id} 客户拒领（{note}），{len(pages)} 张已逐张作废")
        return {"result": "refused", "note": note, "voided": len(pages)}


# ---------------------------------------------------------------- 挂失止付

def stop_payment(conn, booklet_id: int, reason: str, used_qty: int = 0) -> dict:
    """冻结未使用号码。used_qty 为客户已签发张数（自号段起点计）。

    止付迟到：若该簿号码均已作废/销毁，无可冻结号码，置 late=1 并记录台账。
    """
    with tx(conn):
        bk = conn.execute("SELECT * FROM booklets WHERE id=?", (booklet_id,)).fetchone()
        if bk is None:
            raise ServiceError("支票簿不存在")
        pages = conn.execute(
            "SELECT check_no, status FROM check_pages WHERE booklet_id=? ORDER BY check_no",
            (booklet_id,),
        ).fetchall()
        freezable = [
            p["check_no"] for p in pages
            if p["status"] in (config.PAGE_ACTIVE, config.PAGE_ISSUED)
        ]
        # 已使用部分（号段前段）不冻结
        if used_qty > 0:
            freezable = freezable[used_qty:]
        late = 0
        if not freezable:
            late = 1
            cur = conn.execute(
                "INSERT INTO stop_orders(booklet_id, reason, frozen_qty, late, created_at) "
                "VALUES (?,?,?,?,?)",
                (booklet_id, reason, 0, 1, now()),
            )
            log_event(
                conn, "stop",
                f"止付迟到：支票簿 #{booklet_id} 已无可冻结的未使用号码（止付通知 {reason}），止付单 #{cur.lastrowid}",
            )
            return {"stop_id": cur.lastrowid, "frozen_qty": 0, "late": True}

        conn.executemany(
            "UPDATE check_pages SET status=?, updated_at=? WHERE branch_code=? AND check_no=? AND status IN (?,?)",
            [(config.PAGE_STOPPED, now(), bk["branch_code"], n, config.PAGE_ACTIVE, config.PAGE_ISSUED) for n in freezable],
        )
        conn.execute(
            "UPDATE booklets SET status=?, updated_at=? WHERE id=?",
            (config.BK_STOPPED, now(), booklet_id),
        )
        cur = conn.execute(
            "INSERT INTO stop_orders(booklet_id, reason, frozen_qty, late, created_at) VALUES (?,?,?,?,?)",
            (booklet_id, reason, len(freezable), 0, now()),
        )
        log_event(
            conn, "stop",
            f"支票簿 #{booklet_id} 挂失止付：冻结 {len(freezable)} 个未使用号码（{freezable[0]:08d}-{freezable[-1]:08d}，原因：{reason}）",
        )
        return {"stop_id": cur.lastrowid, "frozen_qty": len(freezable), "late": False}


# ---------------------------------------------------------------- 销户交回

def return_blank_pages(conn, booklet_id: int, actual_qty: int, handler: str) -> dict:
    """销户清点：应空白页（在用/止付冻结且未作废）逐张核对，实交数量不符单独标记。"""
    _staff_exists(conn, handler)
    with tx(conn):
        bk = conn.execute("SELECT * FROM booklets WHERE id=?", (booklet_id,)).fetchone()
        if bk is None:
            raise ServiceError("支票簿不存在")
        expected_pages = [
            r["check_no"] for r in conn.execute(
                "SELECT check_no FROM check_pages WHERE booklet_id=? AND status IN (?,?) ORDER BY check_no",
                (booklet_id, config.PAGE_ACTIVE, config.PAGE_STOPPED),
            ).fetchall()
        ]
        expected_qty = len(expected_pages)
        returned = expected_pages[:actual_qty]
        missing = expected_pages[actual_qty:] if actual_qty < expected_qty else []
        extra = max(0, actual_qty - expected_qty)
        mismatch = int(actual_qty != expected_qty)

        conn.execute(
            "INSERT INTO returns(booklet_id, expected_qty, actual_qty, mismatch, handler, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (booklet_id, expected_qty, actual_qty, mismatch, handler, now()),
        )
        for n in returned:
            _void_page_locked(conn, bk["branch_code"], n, booklet_id, "销户交回空白页，逐张打孔作废", handler)

        conn.execute("UPDATE accounts SET status='closed' WHERE account_no=?", (bk["account_no"],))
        if not missing and extra == 0:
            conn.execute(
                "UPDATE booklets SET status=?, updated_at=? WHERE id=?",
                (config.BK_CLOSED, now(), booklet_id),
            )
            log_event(conn, "return", f"支票簿 #{booklet_id} 销户交回 {actual_qty} 张，数量一致，已逐张作废")
        else:
            detail = []
            if missing:
                detail.append(f"少交 {len(missing)} 张（{missing[0]:08d} 等号码未交回，保持原状态并挂账追查）")
            if extra:
                detail.append(f"多交 {extra} 张（无对应号码，实物暂扣）")
            log_event(
                conn, "return",
                f"支票簿 #{booklet_id} 销户交回数量不符：应收 {expected_qty} 实收 {actual_qty}；{'；'.join(detail)}",
            )
        return {"expected_qty": expected_qty, "actual_qty": actual_qty,
                "mismatch": bool(mismatch), "voided": len(returned), "missing": missing}


# ---------------------------------------------------------------- 作废打孔

def void_page(conn, branch_code: str, check_no: int, reason: str, operator: str) -> None:
    _staff_exists(conn, operator)
    with tx(conn):
        _void_page_locked(conn, branch_code, check_no, None, reason, operator)
        log_event(conn, "void", f"号码 {check_no:08d} 手工作废打孔：{reason}（经办 {operator}）")


def _void_page_locked(conn, branch_code: str, check_no: int, booklet_id, reason: str, operator: str) -> None:
    page = conn.execute(
        "SELECT * FROM check_pages WHERE branch_code=? AND check_no=?",
        (branch_code, check_no),
    ).fetchone()
    if page is None:
        raise ServiceError(f"号码 {check_no:08d} 不在 {branch_code} 号码台账中（可能为缺页）")
    if page["status"] == config.PAGE_DESTROYED:
        raise ServiceError(f"号码 {check_no:08d} 已销毁，销毁不可逆，禁止再做任何处理")
    if page["status"] in (config.PAGE_VOID,):
        raise ServiceError(f"号码 {check_no:08d} 已作废，无需重复打孔")
    bid = booklet_id or page["booklet_id"]
    conn.execute(
        "UPDATE check_pages SET status=?, updated_at=? WHERE id=?",
        (config.PAGE_VOID, now(), page["id"]),
    )
    vp = vouchers.void_voucher(conn, branch_code, check_no, reason, operator)
    conn.execute(
        "INSERT INTO voids(branch_code, check_no, booklet_id, reason, operator, voucher_path, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (branch_code, check_no, bid, reason, operator, vp, now()),
    )


# ---------------------------------------------------------------- 销毁批次

def create_destroy_batch(conn, branch_code: str) -> int:
    """把该网点所有作废待销号码汇集为一个开放批次。"""
    with tx(conn):
        rows = conn.execute(
            "SELECT check_no FROM check_pages WHERE branch_code=? AND status=? ORDER BY check_no",
            (branch_code, config.PAGE_VOID),
        ).fetchall()
        if not rows:
            raise ServiceError("没有作废待销毁的号码")
        cur = conn.execute(
            "INSERT INTO destroy_batches(branch_code, status, expected_qty, created_at) VALUES (?,?,?,?)",
            (branch_code, config.BATCH_OPEN, len(rows), now()),
        )
        batch_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO destroy_items(batch_id, branch_code, check_no) VALUES (?,?,?)",
            [(batch_id, branch_code, r["check_no"]) for r in rows],
        )
        log_event(conn, "destroy", f"销毁批次 DB{batch_id:04d} 建立：汇集作废号码 {len(rows)} 张，待双人确认")
        return batch_id


def confirm_destroy(conn, batch_id: int, confirmer_a: str, confirmer_b: str, actual_qty: int) -> dict:
    """双人现场监销确认；两人必须为不同账号；实物数量差异单独挂账。"""
    if confirmer_a == confirmer_b:
        raise ServiceError("销毁必须由两名不同账号的员工双人确认")
    _staff_exists(conn, confirmer_a)
    _staff_exists(conn, confirmer_b)
    with tx(conn):
        batch = conn.execute("SELECT * FROM destroy_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise ServiceError("销毁批次不存在")
        if batch["status"] == config.BATCH_CONFIRMED:
            raise ServiceError(f"批次 DB{batch_id:04d} 已确认销毁，销毁不可逆，禁止重复确认")
        items = conn.execute(
            "SELECT check_no FROM destroy_items WHERE batch_id=? ORDER BY check_no", (batch_id,)
        ).fetchall()
        expected = len(items)
        discrepancy = actual_qty != expected
        status = config.BATCH_DISCREPANCY if discrepancy else config.BATCH_CONFIRMED

        conn.execute(
            "UPDATE destroy_batches SET status=?, actual_qty=?, confirmer_a=?, confirmer_b=?, confirmed_at=? WHERE id=?",
            (status, actual_qty, confirmer_a, confirmer_b, now(), batch_id),
        )
        if discrepancy:
            # 销毁差异：不执行销毁，全部留场复核，台账登记差异
            log_event(
                conn, "destroy",
                f"批次 DB{batch_id:04d} 销毁差异：应销 {expected} 张，现场清点 {actual_qty} 张，"
                f"双人 {confirmer_a}/{confirmer_b} 拒绝签认，批次挂账待查",
            )
            return {"status": status, "expected": expected, "actual": actual_qty, "destroyed": 0}

        conn.executemany(
            "UPDATE destroy_items SET destroyed=1 WHERE batch_id=? AND check_no=?",
            [(batch_id, r["check_no"]) for r in items],
        )
        conn.executemany(
            "UPDATE check_pages SET status=?, updated_at=? WHERE branch_code=? AND check_no=?",
            [(config.PAGE_DESTROYED, now(), batch["branch_code"], r["check_no"]) for r in items],
        )
        # 号码全部销毁的支票簿置为终态
        conn.execute(
            "UPDATE booklets SET status=?, updated_at=? WHERE id IN ("
            "  SELECT b.id FROM booklets b WHERE b.branch_code=? AND NOT EXISTS ("
            "    SELECT 1 FROM check_pages p WHERE p.booklet_id=b.id AND p.status<>?))",
            (config.BK_DESTROYED, now(), batch["branch_code"], config.PAGE_DESTROYED),
        )
        vp = vouchers.destroy_voucher(conn, batch_id, batch["branch_code"], expected, confirmer_a, confirmer_b)
        log_event(
            conn, "destroy",
            f"批次 DB{batch_id:04d} 双人确认销毁 {expected} 张（{confirmer_a}/{confirmer_b}），凭证 {vp}",
        )
        return {"status": status, "expected": expected, "actual": actual_qty,
                "destroyed": expected, "voucher": vp}


def activate_page_guard(conn, branch_code: str, check_no: int) -> None:
    """启用守卫：销毁终态号码永不可再次启用（销毁不可逆）。"""
    page = conn.execute(
        "SELECT status FROM check_pages WHERE branch_code=? AND check_no=?",
        (branch_code, check_no),
    ).fetchone()
    if page and page["status"] == config.PAGE_DESTROYED:
        raise ServiceError(f"号码 {check_no:08d} 已销毁，不可再次启用")


# ---------------------------------------------------------------- 号段条带

def segment_bands(numbers: list[int]) -> list[tuple[int, int]]:
    """把号码列表压缩为连续条带 [(起, 止), ...]，缺口天然断开（供页面渲染）。"""
    if not numbers:
        return []
    bands: list[tuple[int, int]] = []
    start = prev = numbers[0]
    for n in numbers[1:]:
        if n == prev + 1:
            prev = n
        else:
            bands.append((start, prev))
            start = prev = n
    bands.append((start, prev))
    return bands
