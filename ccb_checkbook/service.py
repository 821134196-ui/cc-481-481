# -*- coding: utf-8 -*-
"""业务服务层：支票簿全生命周期状态机。

状态流转
--------
    DRAFT ──审核通过──▶ RESERVED ──双人出库──▶ RELEASED ──签收──▶ ACTIVE
      │                    │                    │                  │
      │驳回                │                    └─拒领/止付─▶ STOP_PAY
      ▼                    ▼                                         │
   REJECTED            （释放占用）                             挂失止付
   ACTIVE ──止付──▶ STOP_PAY ──销户交回──▶ PART_VOID/FULLY_VOID
   FULLY_VOID ──作废号入销毁批次（号码立即 DESTROYED）──▶ 批次销毁 ──▶ DESTROYED

关键约束
--------
* 号码在 check_numbers 中 (branch_code, check_no) 唯一，预留即占用；
* 经办/复核、出库双人、销毁双人必须为不同账号（DB CHECK + 服务层双保险）；
* 所有写动作走 BEGIN IMMEDIATE，抢占同一支票簿时仅首笔成功；
* DESTROYED 为号码终态，任何环节不得再次启用。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import db, vouchers
from .db import (
    BusinessError, log, tx,
    BK_DRAFT, BK_RESERVED, BK_RELEASED, BK_ACTIVE, BK_STOP_PAY,
    BK_PART_VOID, BK_FULLY_VOID, BK_DESTROYED, BK_REJECTED,
    ST_RESERVED, ST_RELEASED, ST_ACTIVE, ST_USED, ST_STOP,
    ST_VOID, ST_DESTROYED, ST_REJECTED,
    VOIDABLE, DESTROYABLE, TERMINAL_NUMBER,
    DEST_OPEN, DEST_CONFIRMED, DEST_DESTROYED,
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ref(prefix: str, branch: str, seq: int) -> str:
    return f"{prefix}{branch}{datetime.now():%Y%m%d}{seq:03d}"


def require_distinct(a: str, b: str, what: str = "经办与复核") -> None:
    if not a or not b:
        raise BusinessError(f"{what}双人账号都必须填写")
    if a == b:
        raise BusinessError(f"{what}必须为不同账号，不能由 {a} 一人完成")


class CheckbookService:
    def __init__(self, conn: sqlite3.Connection, voucher_root,
                 acct_adapter, sig_adapter):
        self.conn = conn
        self.vroot = voucher_root
        self.accts = acct_adapter
        self.sigs = sig_adapter

    # ------------------------------------------------------------------ #
    # 基础查询
    # ------------------------------------------------------------------ #
    def get_book(self, book_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM checkbooks WHERE id=?", (book_id,)).fetchone()
        if row is None:
            raise BusinessError(f"支票簿账册 {book_id} 不存在")
        return row

    def numbers(self, book_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM check_numbers WHERE book_id=? ORDER BY check_no",
            (book_id,)).fetchall()  # type: ignore[return-value]

    def _assert_status(self, book: sqlite3.Row, *allowed: str) -> None:
        if book["status"] not in allowed:
            raise BusinessError(
                f"账册 {book['apply_ref']} 当前状态为 {book['status']}，"
                f"该操作要求状态为 {'/'.join(allowed)}")

    def _next_seq(self, like: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM status_log WHERE 1=1").fetchone()
        return int(row["c"]) + 1

    # ------------------------------------------------------------------ #
    # 1. 申请 + 资格核验 + 连续号码段预留
    # ------------------------------------------------------------------ #
    def apply(self, branch_code: str, acct_no: str, count: int,
              teller_id: str) -> sqlite3.Row:
        if not teller_id:
            raise BusinessError("经办柜员账号必填")
        if not isinstance(count, int) or count <= 0:
            raise BusinessError("支票张数必须为正整数")
        cust = self.conn.execute(
            "SELECT * FROM customers WHERE branch_code=? AND acct_no=?",
            (branch_code, acct_no)).fetchone()
        if cust is None:
            raise BusinessError(f"客户账号 {acct_no} 不存在（虚构客户库）")

        elig = self.accts.check(branch_code, acct_no)
        if not elig.ok:
            raise BusinessError(f"资格核验未通过：{elig.reason}")

        with tx(self.conn):
            ref = _ref("AP", branch_code,
                       self.conn.execute("SELECT COUNT(*) c FROM checkbooks").fetchone()["c"] + 1)
            cur = self.conn.execute(
                "INSERT INTO checkbooks(branch_code, acct_no, cust_no, cust_name,"
                " start_no, end_no, status, apply_ref, teller_id)"
                " VALUES (?,?,?,?,0,0,?,?,?)",
                (branch_code, acct_no, cust["cust_no"], cust["cust_name"],
                 BK_DRAFT, ref, teller_id))
            book_id = cur.lastrowid
            start_no, end_no = self._reserve_contiguous(branch_code, book_id, count)
            self.conn.execute(
                "UPDATE checkbooks SET start_no=?, end_no=? WHERE id=?",
                (start_no, end_no, book_id))
            log(self.conn, "BOOK", book_id, "APPLY",
                f"申请 {ref} 账号 {acct_no} 段 {start_no:08d}-{end_no:08d}"
                f" 资格核验 {elig.ref}", teller_id)
        return self.get_book(book_id)

    def _reserve_contiguous(self, branch: str, book_id: int,
                            count: int) -> tuple[int, int]:
        """在库存区间内寻找第一段长度足够、且未被任何账册占用的连续号码。

        缺页（库存断号、已占用号）会打断连续性；找不到即业务异常。
        """
        ranges = self.conn.execute(
            "SELECT start_no, end_no FROM stock_ranges WHERE branch_code=?"
            " ORDER BY start_no", (branch,)).fetchall()
        used = {r["check_no"] for r in self.conn.execute(
            "SELECT check_no FROM check_numbers WHERE branch_code=?", (branch,))}
        for rg in ranges:
            run_start = None
            prev = None
            # 含一个哨兵，保证段尾也能收尾
            seq = list(range(rg["start_no"], rg["end_no"] + 1)) + [None]
            for no in seq:
                free = no is not None and no not in used
                if free:
                    if run_start is None:
                        run_start = no
                    prev = no
                else:
                    if run_start is not None and prev - run_start + 1 >= count:
                        return self._insert_reserved(branch, book_id,
                                                     run_start, run_start + count - 1)
                    run_start = None
                    prev = None
        raise BusinessError(
            f"网点 {branch} 库存中不存在 {count} 张连续可用号码"
            f"（存在缺页或库存不足），无法预留")

    def _insert_reserved(self, branch: str, book_id: int,
                         start_no: int, end_no: int) -> tuple[int, int]:
        try:
            self.conn.executemany(
                "INSERT INTO check_numbers(branch_code, check_no, book_id, status)"
                " VALUES (?,?,?,?)",
                [(branch, no, book_id, ST_RESERVED)
                 for no in range(start_no, end_no + 1)])
        except sqlite3.IntegrityError as e:
            raise BusinessError(f"号码段 {start_no:08d}-{end_no:08d} 中存在"
                                f"已被占用号码（重复预留）：{e}") from None
        return start_no, end_no

    # ------------------------------------------------------------------ #
    # 2. 申请审核
    # ------------------------------------------------------------------ #
    def approve(self, book_id: int, reviewer_id: str) -> sqlite3.Row:
        with tx(self.conn):
            book = self.get_book(book_id)
            self._assert_status(book, BK_DRAFT)
            require_distinct(book["teller_id"], reviewer_id, "申请经办与审核")
            self.conn.execute(
                "UPDATE checkbooks SET status=?, reviewer_id=? WHERE id=?",
                (BK_RESERVED, reviewer_id, book_id))
            log(self.conn, "BOOK", book_id, "APPROVE",
                f"{book['apply_ref']} 审核通过，号码段预留完成", reviewer_id)
        return self.get_book(book_id)

    def reject(self, book_id: int, reviewer_id: str, reason: str) -> sqlite3.Row:
        with tx(self.conn):
            book = self.get_book(book_id)
            self._assert_status(book, BK_DRAFT)
            require_distinct(book["teller_id"], reviewer_id, "申请经办与审核")
            # 驳回：预留号码释放为 REJECTED 占位（同号不再二次分配）
            self.conn.execute(
                "UPDATE check_numbers SET status=? WHERE book_id=? AND status=?",
                (ST_REJECTED, book_id, ST_RESERVED))
            self.conn.execute(
                "UPDATE checkbooks SET status=?, reviewer_id=?,"
                " refuse_reason=? WHERE id=?",
                (BK_REJECTED, reviewer_id, f"审核驳回：{reason}", book_id))
            log(self.conn, "BOOK", book_id, "REJECT",
                f"{book['apply_ref']} 驳回：{reason}", reviewer_id)
        return self.get_book(book_id)

    # ------------------------------------------------------------------ #
    # 3. 双人出库
    # ------------------------------------------------------------------ #
    def release(self, book_id: int, teller: str, reviewer: str) -> sqlite3.Row:
        require_distinct(teller, reviewer, "出库经办与复核")
        with tx(self.conn):
            book = self.get_book(book_id)
            self._assert_status(book, BK_RESERVED)  # 抢占：第二笔在此失败
            self.conn.execute(
                "UPDATE checkbooks SET status=?, release_teller=?,"
                " release_reviewer=?, released_at=? WHERE id=?",
                (BK_RELEASED, teller, reviewer, _now(), book_id))
            self.conn.execute(
                "UPDATE check_numbers SET status=? WHERE book_id=? AND status=?",
                (ST_RELEASED, book_id, ST_RESERVED))
            log(self.conn, "BOOK", book_id, "RELEASE",
                f"{book['apply_ref']} 双人出库 经办 {teller} / 复核 {reviewer}",
                f"{teller}+{reviewer}")
        return self.get_book(book_id)

    # ------------------------------------------------------------------ #
    # 4. 客户签收 / 拒领
    # ------------------------------------------------------------------ #
    def refuse(self, book_id: int, teller: str, reason: str) -> sqlite3.Row:
        if not reason:
            raise BusinessError("拒领原因必填")
        with tx(self.conn):
            book = self.get_book(book_id)
            self._assert_status(book, BK_RELEASED)
            self.conn.execute(
                "UPDATE checkbooks SET status=?, refused_at=?, refuse_reason=?"
                " WHERE id=?", (BK_STOP_PAY, _now(), reason, book_id))
            self.conn.execute(
                "UPDATE check_numbers SET status=? WHERE book_id=? AND status=?",
                (ST_STOP, book_id, ST_RELEASED))
            path = vouchers.refusal(self.vroot, book["apply_ref"],
                                    book["branch_code"], book["acct_no"],
                                    reason, teller)
            log(self.conn, "BOOK", book_id, "REFUSE",
                f"客户拒领：{reason} 凭证 {path}", teller)
        return self.get_book(book_id)

    def sign(self, book_id: int, force_pass: bool | None = None) -> sqlite3.Row:
        """客户签收：先核对模拟签名，成功才允许启用。重复签收直接拒绝。"""
        with tx(self.conn):
            book = self.get_book(book_id)
            if book["status"] == BK_ACTIVE:
                raise BusinessError(
                    f"{book['apply_ref']} 已签收启用，不能重复签收")
            if book["status"] == BK_DESTROYED:
                raise BusinessError(f"{book['apply_ref']} 已销毁，不能签收")
            self._assert_status(book, BK_RELEASED)  # STOP_PAY 在此被拦（止付阻断）
            sig = self.sigs.verify(book["acct_no"])
            if force_pass is not None:
                sig.matched = force_pass
                sig.reason = "人工核验通过" if force_pass else "签样不符"
            if not sig.matched:
                log(self.conn, "BOOK", book_id, "SIGN_FAIL",
                    f"签收失败：{sig.reason} ({sig.ref})", "CLIENT")
                raise BusinessError(f"签收失败：{sig.reason}")
            self.conn.execute(
                "UPDATE checkbooks SET status=?, signed_at=? WHERE id=?",
                (BK_ACTIVE, _now(), book_id))
            self.conn.execute(
                "UPDATE check_numbers SET status=? WHERE book_id=? AND status=?",
                (ST_ACTIVE, book_id, ST_RELEASED))
            path = vouchers.signoff(
                self.vroot, book["apply_ref"], book["branch_code"],
                book["acct_no"], book["cust_name"],
                (book["start_no"], book["end_no"]), sig.ref, sig.confidence,
                book["release_teller"], book["release_reviewer"])
            log(self.conn, "BOOK", book_id, "SIGN",
                f"客户签收启用 {sig.ref} 凭证 {path}", "CLIENT")
        return self.get_book(book_id)

    # ------------------------------------------------------------------ #
    # 5. 挂失止付
    # ------------------------------------------------------------------ #
    def mark_used(self, book_id: int, check_no: int) -> None:
        """模拟客户已签发使用某张支票（该号不参与交回/销毁）。"""
        with tx(self.conn):
            row = self.conn.execute(
                "SELECT status FROM check_numbers WHERE book_id=? AND check_no=?",
                (book_id, check_no)).fetchone()
            if row is None:
                raise BusinessError(f"号码 {check_no:08d} 不属于该账册")
            if row["status"] != ST_ACTIVE:
                raise BusinessError(
                    f"号码 {check_no:08d} 状态 {row['status']}，非启用状态不能登记使用")
            self.conn.execute(
                "UPDATE check_numbers SET status=? WHERE book_id=? AND check_no=?",
                (ST_USED, book_id, check_no))
            log(self.conn, "NUMBER", f"{book_id}:{check_no}", "USED",
                "客户已签发使用", "CLIENT")

    def stop_payment(self, book_id: int, operator: str, reason: str) -> int:
        """挂失止付：冻结全部未使用号码。返回冻结张数。"""
        if not reason:
            raise BusinessError("止付原因必填")
        with tx(self.conn):
            book = self.get_book(book_id)
            self._assert_status(book, BK_ACTIVE, BK_RELEASED, BK_PART_VOID)
            freezable = self.conn.execute(
                "SELECT COUNT(*) c FROM check_numbers WHERE book_id=?"
                " AND status IN (?,?,?,?)",
                (book_id, ST_RESERVED, ST_RELEASED, ST_ACTIVE, ST_STOP)).fetchone()["c"]
            if freezable == 0:
                raise BusinessError(
                    f"止付迟到：{book['apply_ref']} 已无未使用号码可冻结")
            cur = self.conn.execute(
                "UPDATE check_numbers SET status=? WHERE book_id=?"
                " AND status IN (?,?,?)",
                (ST_STOP, book_id, ST_RESERVED, ST_RELEASED, ST_ACTIVE))
            self.conn.execute(
                "UPDATE checkbooks SET status=?, stop_at=?, stop_reason=? WHERE id=?",
                (BK_STOP_PAY if book["status"] != BK_PART_VOID else BK_PART_VOID,
                 _now(), reason, book_id))
            log(self.conn, "BOOK", book_id, "STOP_PAY",
                f"挂失止付：{reason}，冻结 {cur.rowcount} 张", operator)
            return cur.rowcount

    # ------------------------------------------------------------------ #
    # 6. 销户：空白页交回清点 + 逐张作废打孔
    # ------------------------------------------------------------------ #
    def handover(self, book_id: int, returned_nos: list[int],
                 teller: str, reviewer: str) -> sqlite3.Row:
        require_distinct(teller, reviewer, "交回清点经办与复核")
        with tx(self.conn):
            book = self.get_book(book_id)
            self._assert_status(book, BK_ACTIVE, BK_STOP_PAY, BK_PART_VOID,
                                BK_RELEASED)
            all_nos = self.numbers(book_id)
            voidable = [r for r in all_nos if r["status"] in VOIDABLE]
            expected = len(voidable)
            returned_set = set(returned_nos)
            dup = len(returned_nos) - len(returned_set)
            if dup:
                raise BusinessError(f"交回清单中存在 {dup} 个重复号码")
            unknown = returned_set - {r["check_no"] for r in all_nos}
            if unknown:
                raise BusinessError(
                    "交回号码不属于该账册：" + "、".join(f"{n:08d}" for n in sorted(unknown)))
            already = [r["check_no"] for r in all_nos
                       if r["check_no"] in returned_set and r["status"] not in VOIDABLE]
            if already:
                raise BusinessError(
                    "以下号码已使用/作废/销毁，不能交回："
                    + "、".join(f"{n:08d}" for n in already))

            returned = sorted(returned_set)
            missing_nos = sorted(r["check_no"] for r in voidable
                                 if r["check_no"] not in returned_set)
            # 交回的空白页：逐张作废打孔
            for no in returned:
                self.conn.execute(
                    "UPDATE check_numbers SET status=? WHERE book_id=? AND check_no=?",
                    (ST_VOID, book_id, no))
                path = vouchers.void_slip(
                    self.vroot, book["apply_ref"], book["branch_code"], no,
                    teller, "销户交回空白页，剪角打孔")
                self.conn.execute(
                    "INSERT INTO void_actions(book_id, check_no, teller_id,"
                    " voucher_path) VALUES (?,?,?,?)",
                    (book_id, no, teller, path))
            # 未交回的缺页：维持冻结
            for no in missing_nos:
                self.conn.execute(
                    "UPDATE check_numbers SET status=? WHERE book_id=? AND check_no=?",
                    (ST_STOP, book_id, no))

            cur = self.conn.execute(
                "INSERT INTO handovers(book_id, expected_count, returned_count,"
                " missing_count, status, teller_id, reviewer_id)"
                " VALUES (?,?,?,?,?,?,?)",
                (book_id, expected, len(returned), len(missing_nos),
                 "MATCH" if not missing_nos else "MISMATCH", teller, reviewer))
            hoid = cur.lastrowid
            self.conn.execute(
                "UPDATE void_actions SET handover_id=? WHERE book_id=?"
                " AND handover_id IS NULL", (hoid, book_id))
            path = vouchers.handover(
                self.vroot, hoid, book["apply_ref"], book["branch_code"],
                expected, len(returned), len(missing_nos), missing_nos,
                teller, reviewer)
            self.conn.execute(
                "UPDATE handovers SET voucher_path=? WHERE id=?", (path, hoid))

            new_status = BK_FULLY_VOID if not missing_nos else BK_PART_VOID
            self.conn.execute("UPDATE checkbooks SET status=? WHERE id=?",
                              (new_status, book_id))
            log(self.conn, "HANDOVER", hoid, "HANDOVER",
                f"{book['apply_ref']} 应交 {expected} 实交 {len(returned)}"
                f" 缺页 {len(missing_nos)} "
                f"{'数量相符' if not missing_nos else '数量不符'} 凭证 {path}",
                f"{teller}+{reviewer}")
        return self.get_book(book_id)

    def void_missing(self, book_id: int, check_no: int, teller: str) -> None:
        """缺页后续找回，单张补作废；全部补齐后账册转为 FULLY_VOID。"""
        with tx(self.conn):
            book = self.get_book(book_id)
            self._assert_status(book, BK_PART_VOID, BK_STOP_PAY, BK_ACTIVE)
            row = self.conn.execute(
                "SELECT status FROM check_numbers WHERE book_id=? AND check_no=?",
                (book_id, check_no)).fetchone()
            if row is None:
                raise BusinessError(f"号码 {check_no:08d} 不属于该账册")
            if row["status"] not in VOIDABLE:
                raise BusinessError(
                    f"号码 {check_no:08d} 状态 {row['status']}，不能作废")
            self.conn.execute(
                "UPDATE check_numbers SET status=? WHERE book_id=? AND check_no=?",
                (ST_VOID, book_id, check_no))
            path = vouchers.void_slip(
                self.vroot, book["apply_ref"], book["branch_code"], check_no,
                teller, "缺页找回，补作废打孔")
            self.conn.execute(
                "INSERT INTO void_actions(book_id, check_no, teller_id,"
                " voucher_path) VALUES (?,?,?,?)",
                (book_id, check_no, teller, path))
            outstanding = self.conn.execute(
                "SELECT COUNT(*) c FROM check_numbers WHERE book_id=?"
                " AND status IN (?,?,?,?)",
                (book_id, ST_RESERVED, ST_RELEASED, ST_ACTIVE, ST_STOP)).fetchone()["c"]
            if outstanding == 0:
                self.conn.execute(
                    "UPDATE checkbooks SET status=? WHERE id=?",
                    (BK_FULLY_VOID, book_id))
            log(self.conn, "NUMBER", f"{book_id}:{check_no}", "VOID",
                f"补作废打孔 凭证 {path}", teller)

    # ------------------------------------------------------------------ #
    # 7. 销毁批次
    # ------------------------------------------------------------------ #
    def create_batch(self, branch_code: str) -> sqlite3.Row:
        with tx(self.conn):
            n = self.conn.execute(
                "SELECT COUNT(*) c FROM destroy_batches").fetchone()["c"] + 1
            batch_no = _ref("DB", branch_code, n)
            cur = self.conn.execute(
                "INSERT INTO destroy_batches(branch_code, batch_no, status,"
                " expect_count) VALUES (?,?,?,0)",
                (branch_code, batch_no, DEST_OPEN))
            bid = cur.lastrowid
            log(self.conn, "BATCH", bid, "BATCH_OPEN", f"销毁批次 {batch_no} 建批")
        return self.conn.execute(
            "SELECT * FROM destroy_batches WHERE id=?", (bid,)).fetchone()

    def add_to_batch(self, batch_id: int, book_id: int) -> int:
        """把账册下全部 VOID 号码收入批次；号码即刻 DESTROYED（不可逆）。"""
        with tx(self.conn):
            batch = self._get_batch(batch_id)
            if batch["status"] != DEST_OPEN:
                raise BusinessError(
                    f"批次 {batch['batch_no']} 已{batch['status']}，不能再归集")
            book = self.get_book(book_id)
            if book["branch_code"] != batch["branch_code"]:
                raise BusinessError("不能跨网点归集销毁")
            rows = self.conn.execute(
                "SELECT check_no FROM check_numbers WHERE book_id=? AND status=?",
                (book_id, ST_VOID)).fetchall()
            if not rows:
                raise BusinessError(
                    f"{book['apply_ref']} 没有可销毁的作废号码（须先打孔作废）")
            added = 0
            for r in rows:
                try:
                    self.conn.execute(
                        "INSERT INTO destroy_items(batch_id, branch_code,"
                        " book_id, check_no) VALUES (?,?,?,?)",
                        (batch_id, batch["branch_code"], book_id, r["check_no"]))
                except sqlite3.IntegrityError:
                    raise BusinessError(
                        f"号码 {r['check_no']:08d} 已在其他销毁批次中"
                    ) from None
                self.conn.execute(
                    "UPDATE check_numbers SET status=? WHERE book_id=? AND check_no=?",
                    (ST_DESTROYED, book_id, r["check_no"]))
                added += 1
            self.conn.execute(
                "UPDATE destroy_batches SET expect_count="
                "(SELECT COUNT(*) FROM destroy_items WHERE batch_id=?) WHERE id=?",
                (batch_id, batch_id))
            log(self.conn, "BATCH", batch_id, "BATCH_ADD",
                f"{book['apply_ref']} 入批 {added} 张，号码即刻置 DESTROYED")
            return added

    def confirm_batch(self, batch_id: int, confirm_a: str,
                      confirm_b: str) -> sqlite3.Row:
        """双人确认封存。"""
        require_distinct(confirm_a, confirm_b, "销毁确认")
        with tx(self.conn):
            batch = self._get_batch(batch_id)
            if batch["status"] != DEST_OPEN:
                raise BusinessError(f"批次 {batch['batch_no']} 已封存/销毁")
            cnt = self.conn.execute(
                "SELECT COUNT(*) c FROM destroy_items WHERE batch_id=?",
                (batch_id,)).fetchone()["c"]
            if cnt == 0:
                raise BusinessError("空批次不能封存")
            self.conn.execute(
                "UPDATE destroy_batches SET status=?, confirm_a=?, confirm_b=?,"
                " confirmed_at=?, expect_count=? WHERE id=?",
                (DEST_CONFIRMED, confirm_a, confirm_b, _now(), cnt, batch_id))
            self._write_batch_voucher(batch_id)
            log(self.conn, "BATCH", batch_id, "BATCH_CONFIRM",
                f"双人封存 {cnt} 张：{confirm_a} / {confirm_b}",
                f"{confirm_a}+{confirm_b}")
        return self._get_batch(batch_id)

    def destroy_batch(self, batch_id: int, confirm_a: str, confirm_b: str,
                      actual_count: int, diff_note: str = "") -> sqlite3.Row:
        """实物销毁点数入账。actual 与 expect 不符时必须登记差异说明。"""
        require_distinct(confirm_a, confirm_b, "销毁监销")
        with tx(self.conn):
            batch = self._get_batch(batch_id)
            if batch["status"] == DEST_DESTROYED:
                raise BusinessError(
                    f"批次 {batch['batch_no']} 已销毁，销毁不可逆")
            if batch["status"] != DEST_CONFIRMED:
                raise BusinessError("批次须先经双人确认封存后才能销毁")
            if {confirm_a, confirm_b} != {batch["confirm_a"], batch["confirm_b"]}:
                raise BusinessError("销毁监销双人必须与封存确认人一致")
            expect = batch["expect_count"]
            if actual_count < 0:
                raise BusinessError("实销张数不能为负")
            diff = actual_count - expect
            if diff != 0 and not diff_note.strip():
                raise BusinessError(
                    f"销毁差异 {diff:+d} 张，必须填写差异说明才能入账")
            self.conn.execute(
                "UPDATE destroy_batches SET status=?, destroyed_at=?,"
                " actual_count=?, diff_count=?, diff_note=?, voucher_path=? WHERE id=?",
                (DEST_DESTROYED, _now(), actual_count, diff, diff_note,
                 self._write_batch_voucher(batch_id, actual_count, diff,
                                           diff_note, confirm_a, confirm_b),
                 batch_id))
            # 账册：名下已无未处置号码 → DESTROYED
            for r in self.conn.execute(
                "SELECT DISTINCT book_id FROM destroy_items WHERE batch_id=?",
                    (batch_id,)):
                bid = r["book_id"]
                left = self.conn.execute(
                    "SELECT COUNT(*) c FROM check_numbers WHERE book_id=?"
                    " AND status NOT IN (?,?)", (bid, ST_USED, ST_DESTROYED)
                ).fetchone()["c"]
                if left == 0:
                    self.conn.execute(
                        "UPDATE checkbooks SET status=? WHERE id=?",
                        (BK_DESTROYED, bid))
            log(self.conn, "BATCH", batch_id, "BATCH_DESTROY",
                f"实物销毁 应收 {expect} 实销 {actual_count} 差异 {diff:+d}"
                f"{'（' + diff_note + '）' if diff_note else ''}",
                f"{confirm_a}+{confirm_b}")
        return self._get_batch(batch_id)

    def _get_batch(self, batch_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM destroy_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise BusinessError(f"销毁批次 {batch_id} 不存在")
        return row

    def _write_batch_voucher(self, batch_id: int, actual=None, diff=None,
                             note="", a="", b="") -> str:
        batch = self._get_batch(batch_id)
        items = [(r["book_id"], r["check_no"]) for r in self.conn.execute(
            "SELECT book_id, check_no FROM destroy_items WHERE batch_id=?"
            " ORDER BY book_id, check_no", (batch_id,))]
        return vouchers.destroy(
            self.vroot, batch["batch_no"], batch["branch_code"],
            batch["expect_count"], actual, diff, note,
            a or batch["confirm_a"] or "", b or batch["confirm_b"] or "", items)

    # ------------------------------------------------------------------ #
    # 扫描：待领 / 待销毁（供进程内定时任务调用）
    # ------------------------------------------------------------------ #
    def scan_pending_pickup(self) -> list[sqlite3.Row]:
        """已出库但客户尚未签收（含拒领）的账册。"""
        rows = self.conn.execute(
            "SELECT * FROM checkbooks WHERE status IN (?,?) ORDER BY released_at",
            (BK_RELEASED, BK_STOP_PAY)).fetchall()
        detail = "; ".join(
            f"{r['apply_ref']}({'拒领冻结' if r['status'] == BK_STOP_PAY else '待签收'})"
            for r in rows) or "无"
        self.conn.execute(
            "INSERT INTO scan_runs(kind, detail) VALUES (?,?)",
            ("PENDING_PICKUP", detail))
        return rows

    def scan_pending_destroy(self) -> dict:
        """已打孔待归集 / 批次待确认 / 已封存待销毁。"""
        fully_void = self.conn.execute(
            "SELECT * FROM checkbooks WHERE status=?", (BK_FULLY_VOID,)).fetchall()
        open_batches = self.conn.execute(
            "SELECT * FROM destroy_batches WHERE status=?", (DEST_OPEN,)).fetchall()
        confirmed = self.conn.execute(
            "SELECT * FROM destroy_batches WHERE status=?",
            (DEST_CONFIRMED,)).fetchall()
        detail = (f"待归集账册 {len(fully_void)} 本；待确认批次 {len(open_batches)} 个；"
                  f"待销毁批次 {len(confirmed)} 个")
        self.conn.execute(
            "INSERT INTO scan_runs(kind, detail) VALUES (?,?)",
            ("PENDING_DESTROY", detail))
        return {"fully_void": fully_void, "open_batches": open_batches,
                "confirmed": confirmed}
