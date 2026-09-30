# -*- coding: utf-8 -*-
"""模拟适配器：账户资格核验与签名核对。

真实系统会调用核心银行系统 / 影像签名平台；本系统全部为进程内确定性
模拟，返回虚构结果。特殊账号尾号用于演示异常场景：

* 账户尾号 ``91``：资格核验不通过（账户异常，仅虚构演示）
* 账户尾号 ``92``：签名核对不符（用于“签收失败”）
* 账户状态为 CLOSED：销户，不允许新申请
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class Eligibility:
    ok: bool
    reason: str = ""
    acct_status: str = "NORMAL"
    ref: str = ""                # 核验流水（虚构）


@dataclass
class Signature:
    matched: bool
    confidence: float = 0.0
    ref: str = ""
    reason: str = ""


class AccountAdapter:
    """账户资格适配器（模拟）。"""

    def __init__(self, conn, hook: Optional[Callable[[str], Optional[Eligibility]]] = None):
        self._conn = conn
        self._hook = hook

    def check(self, branch_code: str, acct_no: str) -> Eligibility:
        row = self._conn.execute(
            "SELECT status FROM customers WHERE branch_code=? AND acct_no=?",
            (branch_code, acct_no),
        ).fetchone()
        if row is None:
            return Eligibility(False, "账户不存在（核心系统无此账号，虚构）",
                               acct_status="UNKNOWN",
                               ref=f"EL{branch_code}X{acct_no[-4:]}")
        status = row["status"]
        ref = f"EL{branch_code}{acct_no[-6:]}"
        if status == "CLOSED":
            return Eligibility(False, "账户已销户，不能申领支票簿",
                               acct_status="CLOSED", ref=ref)
        if acct_no.endswith("91"):
            return Eligibility(False, "账户状态异常：存在未结清冻结（虚构演示-91）",
                               acct_status=status, ref=ref)
        if self._hook is not None:
            forced = self._hook(acct_no)
            if forced is not None:
                return forced
        return Eligibility(True, "账户正常，具备支票簿申领资格",
                           acct_status=status, ref=ref)


class SignatureAdapter:
    """客户签名核对适配器（模拟）。"""

    def __init__(self, hook: Optional[Callable[[str], Optional[bool]]] = None):
        self._hook = hook

    def verify(self, acct_no: str, sample: str = "") -> Signature:
        ref = f"SG{acct_no[-8:]}"
        forced = self._hook(acct_no) if self._hook is not None else None
        if forced is True:
            return Signature(True, 0.99, ref, "人工核验通过")
        if forced is False or acct_no.endswith("92"):
            return Signature(False, 0.31, ref, "签样与预留印鉴不符（虚构演示-92）")
        return Signature(True, 0.97, ref, "签样与预留印鉴一致")
