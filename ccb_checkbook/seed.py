# -*- coding: utf-8 -*-
"""演示数据初始化（全部为虚构数据）。

准备的样例：
  1. 正常签收启用一本（ACTIVE）
  2. 挂失止付一本（其中 1 张已使用，其余冻结）
  3. 客户拒领一本（出库后拒领，号码冻结）
  4. 销户交回缺页一本（应交 5 实交 4，PART_VOID / MISMATCH）
  5. 全额作废一本（FULLY_VOID）+ 一个待归集空批次
  6. 已封存待销毁批次一本
  7. 签收失败一本（签样不符，停留在待签收）
  8. 待审核 DRAFT、待出库 RESERVED 各一本
网点库存号码带中预置一段缺页（51-59），用于号码条带展示。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import db as dbmod
from .adapters import AccountAdapter, SignatureAdapter
from .service import CheckbookService

TELLERS = {
    "T001": "柜员-张明（虚构）",
    "T002": "复核-王芳（虚构）",
    "T003": "监销-李强（虚构）",
}


def seed(db_path: str, voucher_root: str, reset: bool = True) -> None:
    if reset:
        for p in (db_path, db_path + "-wal", db_path + "-shm"):
            Path(p).unlink(missing_ok=True)
        vroot = Path(voucher_root)
        if vroot.exists():
            shutil.rmtree(vroot)

    dbmod.init_db(db_path)
    vroot = Path(voucher_root)
    vroot.mkdir(parents=True, exist_ok=True)
    conn = dbmod.connect(db_path)
    svc = CheckbookService(conn, vroot, AccountAdapter(conn), SignatureAdapter())

    try:
        # ---- 网点 ----
        for code, name in [("BJ001", "北京长安支行（虚构）"),
                           ("SH001", "上海浦东支行（虚构）")]:
            conn.execute("INSERT INTO branches(code, name) VALUES (?,?)",
                         (code, name))

        # ---- 虚构客户 ----
        customers = [
            ("C1001", "6217001234560001", "王建国"),
            ("C1002", "6217001234560002", "李晓梅"),
            ("C1003", "6217001234560003", "赵建华"),
            ("C1004", "6217001234560004", "孙丽"),
            ("C1005", "6217001234560005", "周明远"),
            ("C1006", "6217001234560006", "陈秀英"),
            ("C1007", "6217001234560007", "吴桂芳"),
            ("C1092", "6217001234560092", "钱多多"),
        ]
        for cno, acct, name in customers:
            conn.execute(
                "INSERT INTO customers(branch_code, cust_no, acct_no, cust_name)"
                " VALUES ('BJ001',?,?,?)", (cno, acct, name))

        # ---- 号码库存：1-50 与 60-100，中间 51-59 为缺页 ----
        conn.execute(
            "INSERT INTO stock_ranges(branch_code, start_no, end_no)"
            " VALUES ('BJ001',10000001,10000050)")
        conn.execute(
            "INSERT INTO stock_ranges(branch_code, start_no, end_no)"
            " VALUES ('BJ001',10000060,10000100)")
        conn.execute(
            "INSERT INTO stock_ranges(branch_code, start_no, end_no)"
            " VALUES ('SH001',20000001,20000050)")

        def flow(acct, count=5, *, approve=True, release=False, sign=False,
                 used=0, stop=False, refuse_reason=None,
                 handover_nos=None, expect_sign_fail=False):
            book = svc.apply("BJ001", acct, count, "T001")
            bid = book["id"]
            if approve:
                svc.approve(bid, "T002")
            if approve and (release or sign or refuse_reason):
                svc.release(bid, "T001", "T002")
            if refuse_reason:
                svc.refuse(bid, "T001", refuse_reason)
                return book
            if sign:
                if expect_sign_fail:
                    try:
                        svc.sign(bid)
                    except dbmod.BusinessError:
                        pass
                    return book
                svc.sign(bid)
            for i in range(used):
                svc.mark_used(bid, book["start_no"] + i)
            if stop:
                svc.stop_payment(bid, "T001", "客户挂失，支票簿遗失（虚构演示）")
            if handover_nos is not None:
                svc.handover(bid, handover_nos, "T001", "T002")
            return book

        # 1. 正常启用
        b1 = flow("6217001234560001", sign=True)
        # 2. 止付（1 张已用）
        b2 = flow("6217001234560003", sign=True, used=1, stop=True)
        # 3. 拒领
        b3 = flow("6217001234560004", release=True,
                  refuse_reason="客户当场称未申请过支票簿，拒绝领取（虚构）")
        # 4. 缺页交回（5 张只交回 4 张）：先启用并止付，再显式少交 1 张
        b4 = flow("6217001234560005", sign=True, stop=True)
        nums4 = [r["check_no"] for r in svc.numbers(b4["id"])
                 if r["status"] in dbmod.VOIDABLE]
        svc.handover(b4["id"], nums4[:-1], "T001", "T002")  # 缺 1 张
        # 5. 全额作废 → FULLY_VOID
        b5 = flow("6217001234560006", sign=True, stop=True)
        nums5 = [r["check_no"] for r in svc.numbers(b5["id"])
                 if r["status"] in dbmod.VOIDABLE]
        svc.handover(b5["id"], nums5, "T001", "T002")
        # 6. 全额作废并入批 + 双人封存（待销毁）
        b6 = flow("6217001234560007", sign=True, stop=True)
        nums6 = [r["check_no"] for r in svc.numbers(b6["id"])
                 if r["status"] in dbmod.VOIDABLE]
        svc.handover(b6["id"], nums6, "T001", "T002")
        batch_ready = svc.create_batch("BJ001")
        svc.add_to_batch(batch_ready["id"], b6["id"])
        svc.confirm_batch(batch_ready["id"], "T001", "T003")
        # 待归集的空批次
        svc.create_batch("BJ001")
        # 7. 签收失败（签样不符），停留 RELEASED
        flow("6217001234560092", release=True, sign=True, expect_sign_fail=True)
        # 8. 待审核 / 待出库
        flow("6217001234560002", approve=False)
        flow("6217001234560001", approve=True)  # 王建国第二本 RESERVED

        svc.scan_pending_pickup()
        svc.scan_pending_destroy()
        print("演示数据就绪：")
        print(f"  数据库   : {db_path}")
        print(f"  凭证目录 : {voucher_root}")
        print("  样例     : 正常启用 / 挂失止付 / 客户拒领 / 缺页交回 /")
        print("             全额作废 / 待销毁批次 / 签收失败 / 待审核 / 待出库")
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="初始化虚构演示数据")
    ap.add_argument("--db", default="data/checkbook.db")
    ap.add_argument("--vouchers", default="data/vouchers")
    ap.add_argument("--keep", action="store_true", help="不清空既有数据")
    args = ap.parse_args(argv)
    seed(args.db, args.vouchers, reset=not args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
