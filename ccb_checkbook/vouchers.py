# -*- coding: utf-8 -*-
"""虚构凭证：签收单、作废打孔凭证、销毁清单落地为本地文本文件。

仅存放虚构数据，目录默认 ``data/vouchers``，文件名含业务单号避免覆盖。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

HEADER = "中国建设银行（虚构演示系统，非真实凭证）"


def _write(root: Path, sub: str, filename: str, lines: list[str]) -> str:
    d = root / sub
    d.mkdir(parents=True, exist_ok=True)
    path = d / filename
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = [HEADER, f"生成时间：{stamp}", "=" * 48, ""]
    body.extend(lines)
    body.append("")
    body.append("注：本凭证由虚构数据生成，仅用于系统演示。")
    path.write_text("\n".join(body), encoding="utf-8")
    return str(path.relative_to(root.parent)) if root.parent in path.parents else str(path)


def signoff(root: Path, ref: str, branch: str, acct: str, name: str,
            seg: tuple[int, int], sig_ref: str, confidence: float,
            teller: str, reviewer: str) -> str:
    return _write(root, "signoff", f"{ref}_签收.txt", [
        "支票簿签收单",
        f"申请编号：{ref}",
        f"受理网点：{branch}",
        f"客户账号：{acct}    户名：{name}",
        f"号码段　：{seg[0]:08d} - {seg[1]:08d}（共 {seg[1]-seg[0]+1} 张）",
        f"出库经办：{teller}    出库复核：{reviewer}",
        f"签名核对：{sig_ref}  相似度 {confidence:.2f}",
        "客户签字：＿＿＿＿＿＿（虚构）",
    ])


def refusal(root: Path, ref: str, branch: str, acct: str, reason: str,
            teller: str) -> str:
    return _write(root, "refusal", f"{ref}_拒领.txt", [
        "客户拒领登记单",
        f"申请编号：{ref}    网点：{branch}    账号：{acct}",
        f"拒领原因：{reason}",
        f"登记柜员：{teller}",
        "处理方式：号码段维持冻结止付，待客户后续领取或销户交回。",
    ])


def void_slip(root: Path, ref: str, branch: str, check_no: int,
              teller: str, reason: str) -> str:
    return _write(root, "void", f"{ref}_{check_no:08d}_作废.txt", [
        "空白支票作废打孔凭证",
        f"申请编号：{ref}    网点：{branch}",
        f"支票号码：{check_no:08d}",
        f"作废原因：{reason}",
        f"打孔柜员：{teller}",
        "处理方式：剪角打孔，“作废”戳记，入待销毁归集。",
    ])


def handover(root: Path, handover_id: int, ref: str, branch: str,
             expected: int, returned: int, missing: int,
             missing_nos: list[int], teller: str, reviewer: str) -> str:
    status = "数量相符" if missing == 0 and expected == returned else "数量不符"
    lines = [
        "销户空白支票交回清点单",
        f"申请编号：{ref}    网点：{branch}",
        f"应交回：{expected} 张    实交回：{returned} 张",
        f"清点结果：{status}    差异：{missing} 张",
        f"经办柜员：{teller}    复核柜员：{reviewer}",
    ]
    if missing_nos:
        lines.append("缺页号码：" + "、".join(f"{n:08d}" for n in missing_nos))
    return _write(root, "handover", f"HO{handover_id:06d}_{ref}_交回.txt", lines)


def destroy(root: Path, batch_no: str, branch: str, expect: int,
            actual: int | None, diff: int | None, note: str,
            confirm_a: str, confirm_b: str, items: list[tuple[int, int]]) -> str:
    lines = [
        "作废支票销毁批次清单",
        f"批次编号：{batch_no}    网点：{branch}",
        f"归集张数：{expect}",
    ]
    if actual is not None:
        lines += [
            f"实销张数：{actual}    差异：{diff:+d}",
            f"差异说明：{note or '（无）'}",
            f"确认人甲：{confirm_a}    确认人乙：{confirm_b}（双人确认）",
        ]
    else:
        lines += [
            f"确认人甲：{confirm_a or '—'}    确认人乙：{confirm_b or '—'}",
            "状态：待销毁（尚未双人确认封存）",
        ]
    lines.append("-" * 48)
    lines.append("明细（账册 / 号码）：")
    for book_id, no in items:
        lines.append(f"  账册 {book_id:04d}  号码 {no:08d}  [打孔作废]")
    return _write(root, "destroy", f"{batch_no}_销毁批次.txt", lines)
