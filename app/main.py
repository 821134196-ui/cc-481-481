"""本地启动入口：重建虚构数据 → 启动扫描任务 → 启动 Web 服务。

用法：
    python3 -m app.main            # 重新播种（含拒领/缺页/止付样例）并启动
    python3 -m app.main --no-seed  # 保留现有数据直接启动
    python3 -m app.main --port 8010
"""
from __future__ import annotations

import argparse
import sys

from . import config
from .database import connect, init_db
from .scanner import ScanWorker
from .seed import reseed
from .web import make_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="支票簿领用与作废核验系统（虚构演示）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-seed", action="store_true", help="不重建数据，沿用现有数据库")
    parser.add_argument("--scan-interval", type=float, default=20.0)
    args = parser.parse_args(argv)

    if args.no_seed:
        conn = connect()
        init_db(conn)
        conn.close()
        print("沿用现有数据库：", config.DB_PATH)
    else:
        print("重建虚构演示数据（含拒领、缺页、止付样例）…")
        reseed()

    worker = ScanWorker(interval=args.scan_interval, run_immediately=True)
    worker.start()

    server = make_server(args.host, args.port)
    print(f"服务已启动：http://{args.host}:{args.port}/  （Ctrl+C 停止）")
    print("全部数据均为虚构，仅用于流程演示。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
        worker.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
