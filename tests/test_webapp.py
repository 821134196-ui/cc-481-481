# -*- coding: utf-8 -*-
"""HTTP 端到端集成测试：真实起一个 ThreadingHTTPServer，走表单 POST。"""
from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from ccb_checkbook import webapp
from ccb_checkbook.adapters import AccountAdapter, SignatureAdapter
from ccb_checkbook import db as dbmod
from ccb_checkbook.service import CheckbookService
from ccb_checkbook.db import BK_ACTIVE, BK_STOP_PAY, BK_DESTROYED


class WebCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.dbp = str(root / "w.db")
        cls.vroot = root / "vouchers"
        dbmod.init_db(cls.dbp)
        cls.conn = dbmod.connect(cls.dbp)
        cls.svc = CheckbookService(cls.conn, cls.vroot,
                                   AccountAdapter(cls.conn), SignatureAdapter())
        cls.conn.execute("INSERT INTO branches(code,name) VALUES ('BJ001','虚构支行')")
        for i, acct in enumerate([
                "6217001234560001", "6217001234560002", "6217001234560092"], 1):
            cls.conn.execute(
                "INSERT INTO customers(branch_code,cust_no,acct_no,cust_name)"
                " VALUES ('BJ001',?,?,?)", (f"C{i}", acct, f"虚构{i}"))
        cls.conn.execute(
            "INSERT INTO stock_ranges(branch_code,start_no,end_no)"
            " VALUES ('BJ001',1,200)")

        webapp.Handler.app = webapp.App(cls.dbp, cls.vroot)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)
        cls.conn.close()
        cls.tmp.cleanup()

    def get(self, path):
        return urllib.request.urlopen(self.base + path, timeout=5).read().decode()

    def post(self, path, data: dict, expect=303):
        req = urllib.request.Request(
            self.base + path,
            data=urllib.parse.urlencode(data).encode(),
            method="POST")
        # 不跟随 303，直接观察首轮状态码
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            r = opener.open(req, timeout=5)
            self.assertEqual(r.status, expect)
            return r.headers["Location"]
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, expect)
            if expect == 400:
                return None
            return e.headers["Location"]

    def test_pages_render(self):
        for p in ("/", "/apply", "/stock", "/release", "/signoff",
                  "/destroy", "/scans"):
            html = self.get(p + "?branch=BJ001")
            self.assertIn("支票簿", html)

    def test_full_flow_duplicate_sign_and_stop_block(self):
        # 申请
        loc = self.post("/apply?branch=BJ001",
                        {"acct_no": "6217001234560001", "count": "5",
                         "teller": "T001"})
        self.assertIn("/apply", loc)
        bid = self.conn.execute(
            "SELECT MAX(id) m FROM checkbooks").fetchone()["m"]
        self.post(f"/books/{bid}/approve?branch=BJ001", {"reviewer": "T002"})
        self.post(f"/books/{bid}/release?branch=BJ001",
                  {"teller": "T001", "reviewer": "T002"})
        # 抢占：第二笔出库必须 400
        self.post(f"/books/{bid}/release?branch=BJ001",
                  {"teller": "T001", "reviewer": "T003"}, expect=400)
        # 签收启用
        self.post(f"/books/{bid}/sign?branch=BJ001", {})
        self.assertEqual(self.svc.get_book(bid)["status"], BK_ACTIVE)
        # 重复签收 → 400
        self.post(f"/books/{bid}/sign?branch=BJ001", {}, expect=400)
        # 止付
        self.post(f"/books/{bid}/stop?branch=BJ001",
                  {"teller": "T001", "reason": "遗失"})
        self.assertEqual(self.svc.get_book(bid)["status"], BK_STOP_PAY)
        # 止付阻断再次签收 → 400
        self.post(f"/books/{bid}/sign?branch=BJ001", {}, expect=400)

    def test_sign_fail_account_92(self):
        self.post("/apply?branch=BJ001",
                  {"acct_no": "6217001234560092", "count": "3",
                   "teller": "T001"})
        bid = self.conn.execute("SELECT MAX(id) m FROM checkbooks").fetchone()["m"]
        self.post(f"/books/{bid}/approve?branch=BJ001", {"reviewer": "T002"})
        self.post(f"/books/{bid}/release?branch=BJ001",
                  {"teller": "T001", "reviewer": "T002"})
        self.post(f"/books/{bid}/sign?branch=BJ001", {}, expect=400)

    def test_destroy_irreversible_over_http(self):
        self.post("/apply?branch=BJ001",
                  {"acct_no": "6217001234560002", "count": "2",
                   "teller": "T001"})
        bid = self.conn.execute("SELECT MAX(id) m FROM checkbooks").fetchone()["m"]
        self.post(f"/books/{bid}/approve?branch=BJ001", {"reviewer": "T002"})
        self.post(f"/books/{bid}/release?branch=BJ001",
                  {"teller": "T001", "reviewer": "T002"})
        self.post(f"/books/{bid}/sign?branch=BJ001", {})
        self.post(f"/books/{bid}/stop?branch=BJ001",
                  {"teller": "T001", "reason": "销户"})
        nos = "\n".join(f"{r['check_no']:08d}" for r in self.svc.numbers(bid))
        self.post(f"/books/{bid}/handover?branch=BJ001",
                  {"nos": nos, "teller": "T001", "reviewer": "T002"})
        self.post("/batches/create?branch=BJ001", {})
        batch_id = self.conn.execute(
            "SELECT MAX(id) m FROM destroy_batches").fetchone()["m"]
        self.post(f"/batches/{batch_id}/add?branch=BJ001", {"book_id": str(bid)})
        self.post(f"/batches/{batch_id}/confirm?branch=BJ001",
                  {"confirm_a": "T001", "confirm_b": "T003"})
        # 差异无说明 → 400
        self.post(f"/batches/{batch_id}/destroy?branch=BJ001",
                  {"confirm_a": "T001", "confirm_b": "T003",
                   "actual_count": "1"}, expect=400)
        self.post(f"/batches/{batch_id}/destroy?branch=BJ001",
                  {"confirm_a": "T001", "confirm_b": "T003",
                   "actual_count": "1", "diff_note": "差异一张，已复核"})
        self.assertEqual(self.svc.get_book(bid)["status"], BK_DESTROYED)
        # 销毁后任何签收尝试 → 400
        self.post(f"/books/{bid}/sign?branch=BJ001", {}, expect=400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
