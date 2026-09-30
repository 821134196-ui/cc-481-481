# -*- coding: utf-8 -*-
"""支票簿系统测试：全部使用临时目录中的虚构数据。

覆盖：号码唯一、经办复核分离、止付阻断、销毁不可逆、重复签收、
双人抢占、缺页/数量不符、拒领、止付迟到、销毁差异、资格与签样。
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from ccb_checkbook import db as dbmod
from ccb_checkbook.adapters import AccountAdapter, SignatureAdapter
from ccb_checkbook.scanner import ScanWorker
from ccb_checkbook.service import CheckbookService
from ccb_checkbook.db import (
    BusinessError,
    BK_DRAFT, BK_RESERVED, BK_RELEASED, BK_ACTIVE, BK_STOP_PAY,
    BK_PART_VOID, BK_FULLY_VOID, BK_DESTROYED, BK_REJECTED,
    ST_RESERVED, ST_RELEASED, ST_ACTIVE, ST_USED, ST_STOP,
    ST_VOID, ST_DESTROYED, ST_REJECTED,
    DEST_OPEN, DEST_CONFIRMED, DEST_DESTROYED,
)

B = "BJ001"
T1, T2, T3 = "T001", "T002", "T003"


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.dbp = root / "t.db"
        self.vroot = root / "vouchers"
        dbmod.init_db(self.dbp)
        self.conn = dbmod.connect(self.dbp)
        self.svc = CheckbookService(
            self.conn, self.vroot,
            AccountAdapter(self.conn), SignatureAdapter())
        self._refs()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _refs(self):
        c = self.conn
        c.execute("INSERT INTO branches(code,name) VALUES (?,?)",
                  (B, "测试支行（虚构）"))
        accts = ["6217000000000001", "6217000000000002", "6217000000000091",
                 "6217000000000092", "6217000000000003", "6217000000000004",
                 "6217000000000005"]
        for i, a in enumerate(accts, 1):
            c.execute("INSERT INTO customers(branch_code,cust_no,acct_no,"
                      "cust_name) VALUES (?,?,?,?)",
                      (B, f"C{i:04d}", a, f"虚构客户{i}"))
        # 库存 1..50，60..80（51-59 缺页）
        c.execute("INSERT INTO stock_ranges(branch_code,start_no,end_no)"
                  " VALUES (?,?,?)", (B, 1, 50))
        c.execute("INSERT INTO stock_ranges(branch_code,start_no,end_no)"
                  " VALUES (?,?,?)", (B, 60, 80))

    # ---- 便捷流 ----
    def _dummy_book(self):
        """建一本仅用于占用号码的占位账册（绕过业务预留直接插号）。"""
        cur = self.conn.execute(
            "INSERT INTO checkbooks(branch_code,acct_no,cust_no,cust_name,"
            " start_no,end_no,status,apply_ref,teller_id)"
            " VALUES (?,?,?,?,0,0,?,?,?)",
            (B, "6217000000000005", "C0099", "占位", BK_DRAFT,
             f"APDUMMY{self.conn.execute('SELECT COUNT(*) c FROM checkbooks').fetchone()['c']:03d}",
             T1))
        return cur.lastrowid

    def _occupy(self, start: int, end: int) -> int:
        bid = self._dummy_book()
        self.svc._insert_reserved(B, bid, start, end)
        return bid
    def flow(self, acct="6217000000000001", n=5, *, sign=True):
        b = self.svc.apply(B, acct, n, T1)
        self.svc.approve(b["id"], T2)
        self.svc.release(b["id"], T1, T2)
        if sign:
            self.svc.sign(b["id"])
        return self.svc.get_book(b["id"])

    def statuses(self, bid):
        return [r["status"] for r in self.svc.numbers(bid)]


class TestNumberUniqueness(Case):
    def test_db_unique_constraint(self):
        self._occupy(1, 5)
        with self.assertRaises(BusinessError):
            # 另一个账册重复预留同号
            bid = self._dummy_book()
            self.svc._insert_reserved(B, bid, 1, 5)

    def test_sequential_apply_no_overlap(self):
        b1 = self.svc.apply(B, "6217000000000001", 5, T1)
        b2 = self.svc.apply(B, "6217000000000002", 5, T1)
        self.assertEqual((b1["start_no"], b1["end_no"]), (1, 5))
        self.assertEqual((b2["start_no"], b2["end_no"]), (6, 10))

    def test_gap_breaks_contiguity(self):
        # 占掉 1..48，剩 49,50（2 张），下一可用连续段在 60 起
        self._occupy(1, 48)
        b = self.svc.apply(B, "6217000000000001", 3, T1)
        self.assertEqual(b["start_no"], 60)

    def test_insufficient_contiguous(self):
        self._occupy(1, 48)
        with self.assertRaises(BusinessError):  # 60..80 只有 21 张
            self.svc.apply(B, "6217000000000001", 22, T1)

    def test_rejected_numbers_never_reused(self):
        b = self.svc.apply(B, "6217000000000001", 3, T1)
        self.svc.reject(b["id"], T2, "资料不全")
        b2 = self.svc.apply(B, "6217000000000002", 3, T1)
        # 被驳回占位的 1..3 不再二次分配
        self.assertEqual(b2["start_no"], 4)


class TestSeparationOfDuties(Case):
    def test_approve_same_teller_rejected(self):
        b = self.svc.apply(B, "6217000000000001", 3, T1)
        with self.assertRaises(BusinessError):
            self.svc.approve(b["id"], T1)

    def test_release_same_person_rejected(self):
        b = self.svc.apply(B, "6217000000000001", 3, T1)
        self.svc.approve(b["id"], T2)
        with self.assertRaises(BusinessError):
            self.svc.release(b["id"], T2, T2)

    def test_handover_same_person_rejected(self):
        b = self.flow()
        self.svc.stop_payment(b["id"], T1, "挂失")
        nos = [r["check_no"] for r in self.svc.numbers(b["id"])]
        with self.assertRaises(BusinessError):
            self.svc.handover(b["id"], nos, T2, T2)

    def test_destroy_confirm_same_person_rejected(self):
        b = self.flow()
        self.svc.stop_payment(b["id"], T1, "销户")
        nos = [r["check_no"] for r in self.svc.numbers(b["id"])
               if r["status"] in dbmod.VOIDABLE]
        self.svc.handover(b["id"], nos, T1, T2)
        batch = self.svc.create_batch(B)
        self.svc.add_to_batch(batch["id"], b["id"])
        with self.assertRaises(BusinessError):
            self.svc.confirm_batch(batch["id"], T1, T1)

    def test_destroy_witness_must_match_confirmer(self):
        b = self.flow(n=1)
        self.svc.stop_payment(b["id"], T1, "销户")
        nos = [r["check_no"] for r in self.svc.numbers(b["id"])
               if r["status"] in dbmod.VOIDABLE]
        self.svc.handover(b["id"], nos, T1, T2)
        batch = self.svc.create_batch(B)
        self.svc.add_to_batch(batch["id"], b["id"])
        self.svc.confirm_batch(batch["id"], T1, T2)
        with self.assertRaises(BusinessError):  # 监销人与封存人不一致
            self.svc.destroy_batch(batch["id"], T1, T3, 1)


class TestConcurrentRelease(Case):
    def test_only_first_grab_succeeds(self):
        b = self.svc.apply(B, "6217000000000001", 3, T1)
        self.svc.approve(b["id"], T2)
        bid = b["id"]
        results = []

        def grab(teller, reviewer):
            conn = dbmod.connect(self.dbp)
            try:
                svc = CheckbookService(conn, self.vroot,
                                       AccountAdapter(conn), SignatureAdapter())
                try:
                    svc.release(bid, teller, reviewer)
                    results.append("OK")
                except BusinessError as e:
                    results.append(f"FAIL:{e}")
            finally:
                conn.close()

        t_a = threading.Thread(target=grab, args=(T1, T2))
        t_b = threading.Thread(target=grab, args=(T1, T3))
        t_a.start(); t_b.start(); t_a.join(); t_b.join()
        self.assertEqual(sorted(results).count("OK"), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(self.svc.get_book(bid)["status"], BK_RELEASED)


class TestSignAndStop(Case):
    def test_duplicate_sign_rejected(self):
        b = self.flow()
        with self.assertRaises(BusinessError):
            self.svc.sign(b["id"])

    def test_sign_failure_then_manual_pass(self):
        b = self.flow("6217000000000092", sign=False)  # 尾号92 签样不符
        with self.assertRaises(BusinessError):
            self.svc.sign(b["id"])
        self.assertEqual(self.svc.get_book(b["id"])["status"], BK_RELEASED)
        self.svc.sign(b["id"], force_pass=True)
        self.assertEqual(self.svc.get_book(b["id"])["status"], BK_ACTIVE)

    def test_stop_blocks_sign(self):
        b = self.flow(sign=False)
        # 出库后直接止付（挂失于领取前）
        self.svc.stop_payment(b["id"], T1, "客户来电挂失")
        with self.assertRaises(BusinessError):  # 止付阻断签收
            self.svc.sign(b["id"])
        self.assertTrue(all(s == ST_STOP for s in self.statuses(b["id"])))

    def test_stop_freezes_only_unused(self):
        b = self.flow(n=5)
        self.svc.mark_used(b["id"], b["start_no"])  # 首张已用
        frozen = self.svc.stop_payment(b["id"], T1, "遗失")
        self.assertEqual(frozen, 4)
        sts = self.statuses(b["id"])
        self.assertEqual(sts[0], ST_USED)
        self.assertTrue(all(s == ST_STOP for s in sts[1:]))

    def test_late_stop_rejected(self):
        b = self.flow(n=2)
        self.svc.mark_used(b["id"], b["start_no"])
        self.svc.mark_used(b["id"], b["start_no"] + 1)
        with self.assertRaises(BusinessError):  # 全部已用，止付迟到
            self.svc.stop_payment(b["id"], T1, "迟报挂失")

    def test_refusal_freezes_range(self):
        b = self.flow(sign=False)
        self.svc.refuse(b["id"], T1, "客户否认申请")
        self.assertEqual(self.svc.get_book(b["id"])["status"], BK_STOP_PAY)
        self.assertTrue(all(s == ST_STOP for s in self.statuses(b["id"])))
        with self.assertRaises(BusinessError):  # 拒领后不能签收
            self.svc.sign(b["id"])


class TestEligibility(Case):
    def test_account_91_not_eligible(self):
        with self.assertRaises(BusinessError):
            self.svc.apply(B, "6217000000000091", 5, T1)

    def test_unknown_account(self):
        with self.assertRaises(BusinessError):
            self.svc.apply(B, "6217000000009999", 5, T1)


class TestHandoverAndVoid(Case):
    def _stopped_book(self, n=5):
        b = self.flow(n=n)
        self.svc.stop_payment(b["id"], T1, "销户")
        return b

    def test_full_handover_match(self):
        b = self._stopped_book()
        nos = [r["check_no"] for r in self.svc.numbers(b["id"])
               if r["status"] in dbmod.VOIDABLE]
        self.svc.handover(b["id"], nos, T1, T2)
        self.assertEqual(self.svc.get_book(b["id"])["status"], BK_FULLY_VOID)
        self.assertTrue(all(s == ST_VOID for s in self.statuses(b["id"])))
        ho = self.conn.execute(
            "SELECT * FROM handovers WHERE book_id=?", (b["id"],)).fetchone()
        self.assertEqual(ho["status"], "MATCH")

    def test_short_handover_records_mismatch(self):
        b = self._stopped_book()
        nos = [r["check_no"] for r in self.svc.numbers(b["id"])
               if r["status"] in dbmod.VOIDABLE]
        missing = nos[-1:]
        self.svc.handover(b["id"], nos[:-1], T1, T2)  # 少交 1 张
        self.assertEqual(self.svc.get_book(b["id"])["status"], BK_PART_VOID)
        sts = self.statuses(b["id"])
        self.assertEqual(sts[-1], ST_STOP)          # 缺页维持冻结
        self.assertTrue(all(s == ST_VOID for s in sts[:-1]))
        ho = self.conn.execute(
            "SELECT * FROM handovers WHERE book_id=?", (b["id"],)).fetchone()
        self.assertEqual(ho["status"], "MISMATCH")
        self.assertEqual(ho["missing_count"], 1)
        # 缺页找回补作废 → 整本 FULLY_VOID
        self.svc.void_missing(b["id"], missing[0], T1)
        self.assertEqual(self.svc.get_book(b["id"])["status"], BK_FULLY_VOID)

    def test_handover_with_used_number(self):
        b = self.flow(n=3)
        self.svc.mark_used(b["id"], b["start_no"])  # 首张已用
        self.svc.stop_payment(b["id"], T1, "销户")
        nos = [r["check_no"] for r in self.svc.numbers(b["id"])
               if r["status"] in dbmod.VOIDABLE]
        self.assertEqual(len(nos), 2)               # 已用号不要求交回
        self.svc.handover(b["id"], nos, T1, T2)
        sts = self.statuses(b["id"])
        self.assertEqual(sts[0], ST_USED)

    def test_handover_foreign_number_rejected(self):
        b = self._stopped_book()
        with self.assertRaises(BusinessError):
            self.svc.handover(b["id"], [99999999], T1, T2)

    def test_handover_duplicate_listing_rejected(self):
        b = self._stopped_book(n=2)
        nos = [r["check_no"] for r in self.svc.numbers(b["id"])]
        with self.assertRaises(BusinessError):
            self.svc.handover(b["id"], [nos[0], nos[0]], T1, T2)


class TestDestroy(Case):
    def _void_book(self, n=3, acct="6217000000000001"):
        b = self.flow(acct=acct, n=n)
        self.svc.stop_payment(b["id"], T1, "销户")
        nos = [r["check_no"] for r in self.svc.numbers(b["id"])
               if r["status"] in dbmod.VOIDABLE]
        self.svc.handover(b["id"], nos, T1, T2)
        return b

    def test_destroy_is_terminal(self):
        b = self._void_book()
        batch = self.svc.create_batch(B)
        self.svc.add_to_batch(batch["id"], b["id"])
        # 入批即刻 DESTROYED
        self.assertTrue(all(s == ST_DESTROYED for s in self.statuses(b["id"])))
        with self.assertRaises(BusinessError):       # 不能重新启用
            self.svc.sign(b["id"])
        with self.assertRaises(BusinessError):       # 不能再作废
            self.svc.void_missing(b["id"], b["start_no"], T1)

    def test_number_in_one_batch_only(self):
        b1 = self._void_book(n=2, acct="6217000000000001")
        b2 = self._void_book(n=2, acct="6217000000000002")
        x = self.svc.create_batch(B)
        y = self.svc.create_batch(B)
        self.svc.add_to_batch(x["id"], b1["id"])
        self.svc.add_to_batch(y["id"], b2["id"])
        # b1 的号已在批次 x，不能重复归集到 y（y 仍 OPEN）
        with self.assertRaises(BusinessError):
            self.svc.add_to_batch(y["id"], b1["id"])

    def test_add_to_confirmed_batch_rejected(self):
        b = self._void_book()
        batch = self.svc.create_batch(B)
        self.svc.add_to_batch(batch["id"], b["id"])
        self.svc.confirm_batch(batch["id"], T1, T2)
        with self.assertRaises(BusinessError):
            self.svc.add_to_batch(batch["id"], b["id"])

    def test_destroy_diff_requires_note(self):
        b = self._void_book(n=3)
        batch = self.svc.create_batch(B)
        self.svc.add_to_batch(batch["id"], b["id"])
        self.svc.confirm_batch(batch["id"], T1, T2)
        with self.assertRaises(BusinessError):                       # 差异无说明
            self.svc.destroy_batch(batch["id"], T1, T2, 2)
        batch2 = self.svc.destroy_batch(
            batch["id"], T1, T2, 2, "打孔纸屑粘连，现场复核少计 1 张")
        self.assertEqual(batch2["status"], DEST_DESTROYED)
        self.assertEqual(batch2["diff_count"], -1)
        with self.assertRaises(BusinessError):                       # 重复销毁
            self.svc.destroy_batch(batch["id"], T1, T2, 2, "再销")
        self.assertEqual(self.svc.get_book(b["id"])["status"], BK_DESTROYED)

    def test_exact_destroy(self):
        b = self._void_book(n=2)
        batch = self.svc.create_batch(B)
        self.svc.add_to_batch(batch["id"], b["id"])
        self.svc.confirm_batch(batch["id"], T1, T3)
        out = self.svc.destroy_batch(batch["id"], T1, T3, 2)
        self.assertEqual(out["diff_count"], 0)
        self.assertEqual(out["actual_count"], 2)


class TestLifecycleAndScanner(Case):
    def test_full_lifecycle(self):
        b = self.flow(n=5)
        self.assertEqual(b["status"], BK_ACTIVE)
        self.svc.stop_payment(b["id"], T1, "销户")
        nos = [r["check_no"] for r in self.svc.numbers(b["id"])]
        self.svc.handover(b["id"], nos, T1, T2)
        batch = self.svc.create_batch(B)
        self.svc.add_to_batch(batch["id"], b["id"])
        self.svc.confirm_batch(batch["id"], T1, T3)
        self.svc.destroy_batch(batch["id"], T1, T3, 5)
        self.assertEqual(self.svc.get_book(b["id"])["status"], BK_DESTROYED)
        # 全链路只有一个有效状态：同号无重复行
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM (SELECT check_no FROM check_numbers"
            " GROUP BY check_no HAVING COUNT(*)>1)").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_scan_worker(self):
        self.flow(sign=False)          # RELEASED 待签收
        b2 = self.flow(acct="6217000000000003")
        self.svc.stop_payment(b2["id"], T1, "拒")
        logs = []
        w = ScanWorker(self.dbp, self.vroot, interval=60,
                       sink=logs.append)
        w.run_once()
        self.assertTrue(any("待领/拒领 2 本" in m for m in logs))
        runs = self.conn.execute(
            "SELECT kind,COUNT(*) c FROM scan_runs GROUP BY kind").fetchall()
        kinds = {r["kind"]: r["c"] for r in runs}
        self.assertEqual(kinds["PENDING_PICKUP"], 1)
        self.assertEqual(kinds["PENDING_DESTROY"], 1)

    def test_voucher_files_written(self):
        b = self.flow(n=2)
        self.svc.stop_payment(b["id"], T1, "销户")
        nos = [r["check_no"] for r in self.svc.numbers(b["id"])]
        self.svc.handover(b["id"], nos, T1, T2)
        batch = self.svc.create_batch(B)
        self.svc.add_to_batch(batch["id"], b["id"])
        self.svc.confirm_batch(batch["id"], T1, T3)
        kinds = list(sorted(p.name for p in self.vroot.iterdir()))
        self.assertIn("signoff", kinds)
        self.assertIn("void", kinds)
        self.assertIn("handover", kinds)
        self.assertIn("destroy", kinds)
        self.assertTrue(any(self.vroot.glob("destroy/*.txt")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
