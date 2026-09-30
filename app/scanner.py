"""进程内扫描任务：周期性处理“待领”与“待销毁”记录。

- 待领：已出库 issued 的支票簿，模拟客户到场，调用签名适配器完成签收；
- 待销毁：存在作废(void)但尚未归入任何批次的号码时，自动汇集为开放销毁批次。
  销毁的双人确认必须由两名员工在页面上主动完成，扫描任务绝不代为确认。

扫描只把结果写入 event_log 台账，不吞掉业务异常（差异/拒领等交由台账留痕）。
"""
from __future__ import annotations

import threading
import time

from . import config, services
from .database import connect


class ScanWorker(threading.Thread):
    daemon = True

    def __init__(self, interval: float = 20.0, run_immediately: bool = False):
        super().__init__(name="checkbook-scanner")
        self.interval = interval
        self.run_immediately = run_immediately
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not self.run_immediately:
            if self._stop.wait(self.interval):
                return
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:  # 扫描失败不影响主进程
                print(f"[scanner] 扫描异常：{exc}")
            if self._stop.wait(self.interval):
                return

    @staticmethod
    def scan_once() -> dict:
        conn = connect()
        picked = batched = 0
        try:
            # 待领：已出库等待签收的支票簿（签收失败的需人工发起重签，不在此列）
            rows = conn.execute(
                "SELECT id FROM booklets WHERE status=?", (config.BK_ISSUED,)
            ).fetchall()
            for r in rows:
                result = services.customer_signoff(conn, r["id"])
                if result["result"] == "signed":
                    picked += 1
                    services.log_event(conn, "scan", f"待领扫描：支票簿 #{r['id']} 客户已到场签收启用")
                elif result["result"] == "failed":
                    services.log_event(conn, "scan", f"待领扫描：支票簿 #{r['id']} 签收失败，转人工处理")
                else:
                    services.log_event(conn, "scan", f"待领扫描：支票簿 #{r['id']} 客户拒领，已逐张作废")

            # 待销毁：有作废号码且尚无开放批次时汇集建批
            void_rows = conn.execute(
                "SELECT DISTINCT p.branch_code FROM check_pages p WHERE p.status=? "
                "AND NOT EXISTS (SELECT 1 FROM destroy_batches b "
                "WHERE b.branch_code=p.branch_code AND b.status IN (?,?))",
                (config.PAGE_VOID, config.BATCH_OPEN, config.BATCH_DISCREPANCY),
            ).fetchall()
            for r in void_rows:
                batch_id = services.create_destroy_batch(conn, r["branch_code"])
                batched += 1
                services.log_event(conn, "scan", f"待销毁扫描：作废号码已汇集为批次 DB{batch_id:04d}，等待双人确认")
            return {"signed": picked, "batches": batched}
        finally:
            conn.close()
