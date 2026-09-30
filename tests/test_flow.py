"""端到端业务规则测试（全部使用虚构数据，临时数据库隔离）。

覆盖：号码唯一、经办复核分离、止付阻断、销毁不可逆、重复签收、
双柜员抢占仅首笔成功，以及缺页/拒领/交回数量不符/销毁差异等异常分支。
"""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import config, services
from app.database import connect, init_db

BRANCH = "BJ001"


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "test.db"
        self._orig_db = config.DB_PATH
        self._orig_vdir = config.VOUCHER_DIR
        config.DB_PATH = self.db_path
        config.VOUCHER_DIR = root / "vouchers"
        config.VOUCHER_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = self._fresh_db()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self._orig_db
        config.VOUCHER_DIR = self._orig_vdir
        self.tmp.cleanup()

    def _fresh_db(self):
        conn = connect(self.db_path)
        init_db(conn)
        conn.execute("INSERT INTO branches(code,name) VALUES (?,?)", (BRANCH, "虚构测试支行"))
        conn.execute("INSERT INTO customers(customer_id,name,id_tail) VALUES (?,?,?)", ("C1", "虚构客户甲", "0001"))
        conn.execute("INSERT INTO accounts(account_no,customer_id,branch_code) VALUES (?,?,?)",
                     ("ACC1", "C1", BRANCH))
        for acc, name, role in [("T1", "柜员甲", "teller"), ("T2", "柜员乙", "teller"),
                                ("R1", "复核甲", "reviewer"), ("R2", "复核乙", "reviewer"),
                                ("S1", "主管甲", "supervisor"), ("S2", "主管乙", "supervisor")]:
            conn.execute("INSERT INTO staff(account,name,role) VALUES (?,?,?)", (acc, name, role))
        # 库存 10000001..10000020，10000010 物理缺页
        nums = [n for n in range(10_000_001, 10_000_021) if n != 10_000_010]
        conn.executemany(
            "INSERT INTO check_pages(branch_code,check_no,status,updated_at) VALUES (?,?, 'available',?)",
            [(BRANCH, n, services.now()) for n in nums])
        conn.execute("INSERT INTO stock_gaps(branch_code,check_no,reason,created_at) VALUES (?,?,?,?)",
                     (BRANCH, 10_000_010, "虚构缺页", services.now()))
        return conn

    def _approve_one(self, qty, account="ACC1"):
        app_id = services.submit_application(self.conn, BRANCH, account, qty)
        return services.review_application(self.conn, app_id, "R1", True)

    # ---------------------------------------------------------- 号码唯一

    def test_check_number_unique_one_state(self):
        # 全库每个号码只有一行（UNIQUE 约束）
        dupes = self.conn.execute(
            "SELECT branch_code, check_no, COUNT(*) c FROM check_pages GROUP BY branch_code, check_no HAVING c>1"
        ).fetchall()
        self.assertEqual(dupes, [])

        r1 = self._approve_one(5)  # 10000001-05
        self.assertEqual((r1["start_no"], r1["end_no"]), (10_000_001, 10_000_005))
        # 第二本必然拿到不同号码，且预留号码无一重叠
        r2 = self._approve_one(4)
        self.assertEqual((r2["start_no"], r2["end_no"]), (10_000_006, 10_000_009))
        overlap = self.conn.execute(
            "SELECT check_no FROM check_pages WHERE booklet_id=? INTERSECT "
            "SELECT check_no FROM check_pages WHERE booklet_id=?",
            (r1["booklet_id"], r2["booklet_id"])).fetchall()
        self.assertEqual(overlap, [])
        # 同一号码在任意时刻只能有一个状态（结构性保证：一行一状态）
        bad = self.conn.execute(
            "SELECT check_no FROM check_pages GROUP BY branch_code, check_no "
            "HAVING COUNT(DISTINCT status) > 1").fetchall()
        self.assertEqual(bad, [])

    def test_gap_blocks_continuous_segment(self):
        # 缺口两侧最长连续段分别为 9 张（10000001-09）；申请 15 张连续号段失败
        app_id = services.submit_application(self.conn, BRANCH, "ACC1", 15)
        with self.assertRaises(services.ServiceError) as cm:
            services.review_application(self.conn, app_id, "R1", True)
        self.assertIn("连续号码", str(cm.exception))
        # 失败后申请仍待审核，号码一张都没有被预留
        self.assertEqual(
            self.conn.execute("SELECT status FROM applications WHERE id=?", (app_id,)).fetchone()["status"],
            config.APP_PENDING)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM check_pages WHERE status<>'available'").fetchone()["c"], 0)

    # ---------------------------------------------------------- 经办复核分离

    def test_handler_reviewer_must_differ(self):
        bk = self._approve_one(5)["booklet_id"]
        with self.assertRaises(services.ServiceError) as cm:
            services.disburse(self.conn, bk, "T1", "T1", 5)
        self.assertIn("不同员工账号", str(cm.exception))
        # 失败后支票簿仍为预留态
        self.assertEqual(
            self.conn.execute("SELECT status FROM booklets WHERE id=?", (bk,)).fetchone()["status"],
            config.BK_RESERVED)

    def test_physical_qty_mismatch_blocks_disburse(self):
        bk = self._approve_one(5)["booklet_id"]
        with self.assertRaises(services.ServiceError):
            services.disburse(self.conn, bk, "T1", "R1", 4)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM disbursals").fetchone()["c"], 0)

    # ---------------------------------------------------------- 双柜员抢占

    def test_two_tellers_race_only_first_wins(self):
        bk = self._approve_one(5)["booklet_id"]
        barrier = threading.Barrier(2)
        results = {}

        def disburse(who_handler, who_reviewer, slot):
            conn = connect(self.db_path)
            try:
                barrier.wait()
                services.disburse(conn, bk, who_handler, who_reviewer, 5)
                results[slot] = "ok"
            except services.ServiceError as e:
                results[slot] = f"fail:{e}"
            finally:
                conn.close()

        t1 = threading.Thread(target=disburse, args=("T1", "R1", "a"))
        t2 = threading.Thread(target=disburse, args=("T2", "R2", "b"))
        t1.start(); t2.start(); t1.join(); t2.join()

        outcomes = sorted(results.values())
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0][:4], "fail")
        self.assertEqual(outcomes[1], "ok")
        # 号码只被出库一次：没有重复出库记录
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM disbursals WHERE booklet_id=?", (bk,)).fetchone()["c"], 1)
        self.assertEqual(
            self.conn.execute("SELECT status FROM booklets WHERE id=?", (bk,)).fetchone()["status"],
            config.BK_ISSUED)

    # ---------------------------------------------------------- 签收与重复签收

    def test_signoff_activates_and_no_duplicate(self):
        bk = self._approve_one(5)["booklet_id"]
        services.disburse(self.conn, bk, "T1", "R1", 5)
        res = services.customer_signoff(self.conn, bk)
        self.assertEqual(res["result"], "signed")
        self.assertEqual(
            self.conn.execute("SELECT status FROM booklets WHERE id=?", (bk,)).fetchone()["status"],
            config.BK_ACTIVE)
        # 重复签收被拒绝
        with self.assertRaises(services.ServiceError) as cm:
            services.customer_signoff(self.conn, bk)
        self.assertIn("禁止重复签收", str(cm.exception))

    def test_signoff_failure_not_activated_then_resign(self):
        # C77 在适配器里是签名不符；构造专用客户
        self.conn.execute("INSERT INTO customers(customer_id,name,id_tail) VALUES (?,?,?)",
                          ("C-0077", "虚构拒签客户", "0077"))
        self.conn.execute("INSERT INTO accounts(account_no,customer_id,branch_code) VALUES (?,?,?)",
                          ("ACC77", "C-0077", BRANCH))
        app_id = services.submit_application(self.conn, BRANCH, "ACC77", 3)
        bk = services.review_application(self.conn, app_id, "R1", True)["booklet_id"]
        services.disburse(self.conn, bk, "T1", "R1", 3)
        r1 = services.customer_signoff(self.conn, bk)
        self.assertEqual(r1["result"], "failed")
        self.assertEqual(
            self.conn.execute("SELECT status FROM booklets WHERE id=?", (bk,)).fetchone()["status"],
            config.BK_CLAIM_FAILED)
        # 页面仍为 issued（未启用，可重签）
        self.assertEqual(
            self.conn.execute("SELECT DISTINCT status FROM check_pages WHERE booklet_id=?", (bk,)).fetchone()["status"],
            config.PAGE_ISSUED)

    def test_refusal_voids_all_pages(self):
        self.conn.execute("INSERT INTO customers(customer_id,name,id_tail) VALUES (?,?,?)",
                          ("C-0088", "虚构拒领客户", "0088"))
        self.conn.execute("INSERT INTO accounts(account_no,customer_id,branch_code) VALUES (?,?,?)",
                          ("ACC88", "C-0088", BRANCH))
        app_id = services.submit_application(self.conn, BRANCH, "ACC88", 5)
        bk = services.review_application(self.conn, app_id, "R1", True)["booklet_id"]
        services.disburse(self.conn, bk, "T1", "R1", 5)
        r = services.customer_signoff(self.conn, bk)
        self.assertEqual(r["result"], "refused")
        self.assertEqual(r["voided"], 5)
        self.assertEqual(
            self.conn.execute("SELECT status FROM booklets WHERE id=?", (bk,)).fetchone()["status"],
            config.BK_REFUSED)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM check_pages WHERE booklet_id=? AND status='void'",
                              (bk,)).fetchone()["c"], 5)

    # ---------------------------------------------------------- 止付阻断

    def test_stop_payment_freezes_and_blocks(self):
        bk = self._approve_one(8)["booklet_id"]
        services.disburse(self.conn, bk, "T1", "R1", 8)
        services.customer_signoff(self.conn, bk)
        r = services.stop_payment(self.conn, bk, "虚构挂失", used_qty=3)
        self.assertFalse(r["late"])
        self.assertEqual(r["frozen_qty"], 5)
        # 前 3 张保持 active，后 5 张被冻结
        states = {row["check_no"]: row["status"] for row in self.conn.execute(
            "SELECT check_no,status FROM check_pages WHERE booklet_id=?", (bk,))}
        active = [n for n, s in states.items() if s == config.PAGE_ACTIVE]
        stopped = [n for n, s in states.items() if s == config.PAGE_STOPPED]
        self.assertEqual(len(active), 3)
        self.assertEqual(len(stopped), 5)
        # 止付阻断：已止付的支票簿不能再签收/启用、不能再出库
        with self.assertRaises(services.ServiceError):
            services.customer_signoff(self.conn, bk)
        with self.assertRaises(services.ServiceError):
            services.disburse(self.conn, bk, "T2", "R2", 8)
        # 重复作废一张止付冻结页在销户流程之外：手工作废允许（物理处置），但已作废页不可重复作废
        services.void_page(self.conn, BRANCH, stopped[0], "虚构单张打孔", "T1")
        with self.assertRaises(services.ServiceError):
            services.void_page(self.conn, BRANCH, stopped[0], "重复打孔", "T1")

    def test_late_stop_after_void(self):
        # 拒领全部作废后再止付 → 止付迟到，零冻结
        self.conn.execute("INSERT INTO customers(customer_id,name,id_tail) VALUES (?,?,?)",
                          ("C-0088", "虚构拒领客户", "0088"))
        self.conn.execute("INSERT INTO accounts(account_no,customer_id,branch_code) VALUES (?,?,?)",
                          ("ACC88", "C-0088", BRANCH))
        app_id = services.submit_application(self.conn, BRANCH, "ACC88", 4)
        bk = services.review_application(self.conn, app_id, "R1", True)["booklet_id"]
        services.disburse(self.conn, bk, "T1", "R1", 4)
        services.customer_signoff(self.conn, bk)
        r = services.stop_payment(self.conn, bk, "迟到的止付通知")
        self.assertTrue(r["late"])
        self.assertEqual(r["frozen_qty"], 0)

    # ---------------------------------------------------------- 交回数量不符

    def test_return_quantity_mismatch(self):
        bk = self._approve_one(6)["booklet_id"]
        services.disburse(self.conn, bk, "T1", "R1", 6)
        services.customer_signoff(self.conn, bk)
        # 应收 6 张，实交 4 张：只作废交回的 4 张，2 张保持 active 挂账
        r = services.return_blank_pages(self.conn, bk, 4, "T1")
        self.assertTrue(r["mismatch"])
        self.assertEqual(r["voided"], 4)
        self.assertEqual(len(r["missing"]), 2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM check_pages WHERE booklet_id=? AND status='void'",
                              (bk,)).fetchone()["c"], 4)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM check_pages WHERE booklet_id=? AND status='active'",
                              (bk,)).fetchone()["c"], 2)

    # ---------------------------------------------------------- 销毁不可逆

    def test_destroy_double_confirm_and_irreversible(self):
        bk = self._approve_one(5)["booklet_id"]
        services.disburse(self.conn, bk, "T1", "R1", 5)
        services.customer_signoff(self.conn, bk)
        r = services.return_blank_pages(self.conn, bk, 5, "T1")
        self.assertFalse(r["mismatch"])

        batch_id = services.create_destroy_batch(self.conn, BRANCH)
        # 两名确认人必须不同
        with self.assertRaises(services.ServiceError):
            services.confirm_destroy(self.conn, batch_id, "S1", "S1", 5)
        res = services.confirm_destroy(self.conn, batch_id, "S1", "S2", 5)
        self.assertEqual(res["status"], config.BATCH_CONFIRMED)
        self.assertEqual(res["destroyed"], 5)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM check_pages WHERE status='destroyed'").fetchone()["c"], 5)

        # 不可逆：重复确认、作废、启用全部被拒
        with self.assertRaises(services.ServiceError) as cm:
            services.confirm_destroy(self.conn, batch_id, "S1", "S2", 5)
        self.assertIn("不可逆", str(cm.exception))
        with self.assertRaises(services.ServiceError):
            services.void_page(self.conn, BRANCH, 10_000_001, "再打孔", "T1")
        with self.assertRaises(services.ServiceError):
            services.activate_page_guard(self.conn, BRANCH, 10_000_001)

    def test_destroy_discrepancy_holds_batch(self):
        bk = self._approve_one(5)["booklet_id"]
        services.disburse(self.conn, bk, "T1", "R1", 5)
        services.customer_signoff(self.conn, bk)
        services.return_blank_pages(self.conn, bk, 5, "T1")
        batch_id = services.create_destroy_batch(self.conn, BRANCH)
        res = services.confirm_destroy(self.conn, batch_id, "S1", "S2", 4)  # 现场少一张
        self.assertEqual(res["status"], config.BATCH_DISCREPANCY)
        # 差异不销毁：号码仍是 void，批次可再次确认
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM check_pages WHERE status='void'").fetchone()["c"], 5)
        res2 = services.confirm_destroy(self.conn, batch_id, "S2", "S1", 5)
        self.assertEqual(res2["status"], config.BATCH_CONFIRMED)


if __name__ == "__main__":
    unittest.main()
