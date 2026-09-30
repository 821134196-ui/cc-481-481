"""本地凭证文件：在 data/vouchers 下生成虚构的签收、作废、销毁凭证。

凭证为纯文本（带红色印章标记，由页面渲染为印章红），不包含任何真实客户信息。
凭证索引写入复用业务事务的同一连接，避免在持锁事务中另开连接等待。
"""
from __future__ import annotations

from datetime import datetime

from . import config


def _write(kind: str, ref_no: str, body: str, conn=None) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_ref = ref_no.replace("/", "-").replace(" ", "_")
    path = config.VOUCHER_DIR / f"{kind}_{safe_ref}_{ts}.txt"
    stamp = "★"
    content = (
        "中国建设银行（虚构演示网点）\n"
        "====================================\n"
        f"凭证类型：{kind_cn(kind)}\n"
        f"凭证编号：{ref_no}\n"
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        "------------------------------------\n"
        f"{body}\n"
        "------------------------------------\n"
        "本凭证全部由虚构数据生成，仅用于流程演示\n"
        f"              [{stamp} 业务讫章（虚构） {stamp}]\n"
    )
    path.write_text(content, encoding="utf-8")
    # 只存文件名；查看时由 web 层在凭证目录内解析（防穿越）
    rel = path.name
    if conn is not None:
        conn.execute(
            "INSERT INTO vouchers(kind, ref_no, path, created_at) VALUES (?,?,?,?)",
            (kind, ref_no, rel, datetime.now().isoformat(timespec="seconds")),
        )
    return rel


def kind_cn(kind: str) -> str:
    return {"signoff": "客户签收凭证", "void": "支票作废（打孔）凭证", "destroy": "支票销毁凭证"}.get(kind, kind)


def signoff_voucher(conn, booklet_id: int, branch: str, start_no: int, end_no: int, qty: int, customer: str, result: str, note: str) -> str:
    body = (
        f"网点：{branch}\n支票簿号段：{start_no:08d} - {end_no:08d}（共 {qty} 张）\n"
        f"签收结果：{result_cn(result)}\n客户签字（虚构）：{customer} 已阅知\n适配器说明：{note}\n"
    )
    return _write("signoff", f"BK{booklet_id:04d}", body, conn)


def void_voucher(conn, branch: str, check_no: int, reason_cn: str, operator: str) -> str:
    body = (
        f"网点：{branch}\n支票号码：{check_no:08d}\n"
        f"作废方式：沿撕线打孔并加盖「作废」戳记\n作废原因：{reason_cn}\n经办：{operator}\n"
    )
    return _write("void", f"{branch}-{check_no:08d}", body, conn)


def destroy_voucher(conn, batch_id: int, branch: str, qty: int, a: str, b: str) -> str:
    body = (
        f"网点：{branch}\n销毁批次：DB{batch_id:04d}\n销毁张数：{qty}\n"
        f"双人确认：{a} ／ {b}（两人账号不同，均已现场监销）\n"
    )
    return _write("destroy", f"DB{batch_id:04d}", body, conn)


def result_cn(result: str) -> str:
    return {"signed": "签收成功", "failed": "签收失败", "refused": "客户拒领"}.get(result, result)
