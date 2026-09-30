"""虚构演示数据播种：网点、客户、账户、员工、号码库存与四类样例。

启动命令（run.sh）每次先重建数据库，再播种：
- 拒领样例：客户 C-0088 拒领，5 张已逐张作废，等待进入销毁批次；
- 缺页样例：库存号段在 10000050 处物理缺页，55 张申请无法取得连续号段；
- 止付样例：客户 C-0003 挂失止付，3 张已用、5 张未使用号码被冻结；
- 另含签收失败样例（C-0077 签名比对不符）与待签收/待销毁记录供扫描任务处理。
"""
from __future__ import annotations

from . import config, services
from .database import connect, init_db

BRANCH = "BJ001"
STOCK_START = 10_000_001
STOCK_END = 10_000_100
GAP_NO = 10_000_050  # 虚构物理缺页

BRANCHES = [(BRANCH, "北京西长安街支行（虚构）")]

CUSTOMERS = [
    ("C-0001", "张明远", "3217"),
    ("C-0002", "林婉清", "0842"),
    ("C-0003", "周承宇", "5519"),  # 止付样例
    ("C-0077", "许嘉树", "7763"),  # 签收失败样例
    ("C-0088", "顾南乔", "8820"),  # 拒领样例
    ("C-0099", "沈知微", "9936"),  # 资格不通过样例
    ("C-0011", "陆景明", "1188"),  # 大额申请（被缺页阻断）
]

ACCOUNTS = [
    ("6217 0011 0001", "C-0001"),
    ("6217 0011 0002", "C-0002"),
    ("6217 0011 0003", "C-0003"),
    ("6217 0011 0077", "C-0077"),
    ("6217 0011 0088", "C-0088"),
    ("6217 0011 0099", "C-0099"),
    ("6217 0011 0011", "C-0011"),
]

STAFF = [
    ("T001", "柜员甲", "teller"),
    ("T002", "柜员乙", "teller"),
    ("R001", "复核员甲", "reviewer"),
    ("R002", "复核员乙", "reviewer"),
    ("S001", "营运主管甲", "supervisor"),
    ("S002", "营运主管乙", "supervisor"),
]


def reseed(db_path=config.DB_PATH) -> None:
    # 连同 WAL/SHM 一起重建，保证“一条启动命令”得到确定样例
    for suffix in ("", "-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix)
        if p.exists():
            p.unlink()
    # 清空上一轮的虚构凭证，避免与新库中的凭证索引不一致
    if config.VOUCHER_DIR.exists():
        for f in config.VOUCHER_DIR.glob("*.txt"):
            f.unlink()
    config.VOUCHER_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    init_db(conn)
    try:
        conn.executemany("INSERT INTO branches(code, name) VALUES (?,?)", BRANCHES)
        conn.executemany("INSERT INTO customers(customer_id, name, id_tail) VALUES (?,?,?)", CUSTOMERS)
        conn.executemany(
            "INSERT INTO accounts(account_no, customer_id, branch_code) VALUES (?,?,?)",
            [(a, c, BRANCH) for a, c in ACCOUNTS],
        )
        conn.executemany("INSERT INTO staff(account, name, role) VALUES (?,?,?)", STAFF)

        # 号码库存：跳过物理缺页号
        pages = [
            (BRANCH, n) for n in range(STOCK_START, STOCK_END + 1) if n != GAP_NO
        ]
        conn.executemany(
            "INSERT INTO check_pages(branch_code, check_no, status, updated_at) "
            "VALUES (?,?, 'available', ?)",
            [(b, n, services.now()) for b, n in pages],
        )
        conn.execute(
            "INSERT INTO stock_gaps(branch_code, check_no, reason, created_at) VALUES (?,?,?,?)",
            (BRANCH, GAP_NO, "入库验收发现纸页残缺，登记缺页（虚构）", services.now()),
        )

        # 样例一：已审核预留，待双人出库
        a1 = services.submit_application(conn, BRANCH, "6217 0011 0001", 10)
        services.review_application(conn, a1, "R001", True)

        # 样例二：已双人出库，待客户签收（扫描任务将自动完成签收）
        a2 = services.submit_application(conn, BRANCH, "6217 0011 0002", 10)
        bk2 = services.review_application(conn, a2, "R001", True)["booklet_id"]
        services.disburse(conn, bk2, "T001", "R001", 10)

        # 样例三：客户拒领 → 5 张逐张作废（扫描任务将汇集为销毁批次）
        a3 = services.submit_application(conn, BRANCH, "6217 0011 0088", 5)
        bk3 = services.review_application(conn, a3, "R002", True)["booklet_id"]
        services.disburse(conn, bk3, "T002", "R002", 5)
        services.customer_signoff(conn, bk3)

        # 样例四：签收启用后挂失止付（已签发 3 张，冻结剩余 5 张）
        a4 = services.submit_application(conn, BRANCH, "6217 0011 0003", 8)
        bk4 = services.review_application(conn, a4, "R001", True)["booklet_id"]
        services.disburse(conn, bk4, "T001", "R002", 8)
        services.customer_signoff(conn, bk4)
        services.stop_payment(conn, bk4, "客户电话挂失，支票簿遗失（虚构）", used_qty=3)

        # 样例五：签收失败（签名不符），保留待重新签收
        a5 = services.submit_application(conn, BRANCH, "6217 0011 0077", 4)
        bk5 = services.review_application(conn, a5, "R002", True)["booklet_id"]
        services.disburse(conn, bk5, "T002", "R001", 4)
        services.customer_signoff(conn, bk5)

        # 样例六：大额申请待审核——通过时将被缺页阻断（最长连续段仅 50 张）
        services.submit_application(conn, BRANCH, "6217 0011 0011", 55)

        services.log_event(conn, "scan", "演示数据播种完成：含拒领、缺页、止付、签收失败样例（全部虚构）")
    finally:
        conn.close()


if __name__ == "__main__":
    reseed()
    print("虚构演示数据已播种到", config.DB_PATH)
