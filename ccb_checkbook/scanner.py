# -*- coding: utf-8 -*-
"""进程内定时扫描任务：周期清点“待领”与“待销毁”记录。

使用标准库 threading 守护线程，每次扫描使用独立 SQLite 连接
（sqlite3 连接不跨线程复用）。扫描结果写入 scan_runs 表并可回调通知。
"""
from __future__ import annotations

import threading
from pathlib import Path

from . import db as dbmod
from .adapters import AccountAdapter, SignatureAdapter
from .service import CheckbookService


class ScanWorker:
    def __init__(self, db_path, voucher_root, interval: float = 30.0,
                 sink=print):
        self.db_path = str(db_path)
        self.voucher_root = Path(voucher_root)
        self.interval = interval
        self.sink = sink
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ticks = 0

    def _service(self) -> CheckbookService:
        conn = dbmod.connect(self.db_path)
        return CheckbookService(conn, self.voucher_root,
                                AccountAdapter(conn), SignatureAdapter())

    def run_once(self) -> None:
        svc = self._service()
        try:
            pickup = svc.scan_pending_pickup()
            pending = svc.scan_pending_destroy()
            self.ticks += 1
            self.sink(
                f"[扫描#{self.ticks}] 待领/拒领 {len(pickup)} 本；"
                f"待归集 {len(pending['fully_void'])} 本、"
                f"待确认批次 {len(pending['open_batches'])} 个、"
                f"待销毁批次 {len(pending['confirmed'])} 个")
        finally:
            svc.conn.close()

    def _loop(self) -> None:
        # 启动后先跑一次，再按周期跑
        self.run_once()
        while not self._stop.wait(self.interval):
            try:
                self.run_once()
            except Exception as e:  # 扫描异常不能拖垮主进程
                self.sink(f"[扫描] 本轮异常：{e}")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="checkbook-scanner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
