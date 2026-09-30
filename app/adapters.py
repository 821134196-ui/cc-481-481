"""模拟适配器：返回账户资格与客户签名结果。

真实系统会对接核心系统与电子签名平台；这里全部使用虚构数据，
通过预置的“场景标记”稳定地返回不同结果，便于演示异常分支。
"""
from __future__ import annotations

# 虚构场景标记：客户代号后缀决定资格与签收表现
SCENARIO_TAGS = {
    "正常": "qualify_ok / sign_ok",
    "缺页样例": "库存号段含物理缺页",
    "拒领样例": "客户拒领",
    "止付样例": "签收后挂失止付",
}

# 客户代号 -> 资格核验结果
QUALIFY_OVERRIDE = {
    # 虚构：账户冻结/销户意向的客户不具备领用资格
    "C-0099": (False, "账户存在未结冻结事项，核验不通过"),
}

# 客户代号 -> 签收适配器结果：signed / failed / refused
SIGN_OVERRIDE = {
    "C-0077": ("failed", "签名比对不符（模拟：相似度 62%）"),
    "C-0088": ("refused", "客户现场拒领，预约再次联系"),
}


def qualify_account(conn, account_no: str) -> tuple[bool, str]:
    """查询账户资格：账户须存在、状态正常，且无适配器侧的拒绝标记。"""
    row = conn.execute(
        "SELECT a.status, a.customer_id, c.name FROM accounts a "
        "JOIN customers c ON c.customer_id = a.customer_id WHERE a.account_no = ?",
        (account_no,),
    ).fetchone()
    if row is None:
        return False, "账户不存在（虚构数据）"
    if row["status"] != "normal":
        return False, f"账户状态为{row['status']}，不可领用支票"
    override = QUALIFY_OVERRIDE.get(row["customer_id"])
    if override:
        return override
    return True, f"账户状态正常，客户「{row['name']}」资格核验通过"


def request_signature(conn, account_no: str) -> tuple[str, str]:
    """模拟电子签名适配器：返回 (signed|failed|refused, 说明)。"""
    row = conn.execute("SELECT customer_id FROM accounts WHERE account_no = ?", (account_no,)).fetchone()
    if row is None:
        return "failed", "未找到账户"
    tag = SIGN_OVERRIDE.get(row["customer_id"])
    if tag:
        return tag
    return "signed", "签名与预留印鉴一致（模拟：相似度 96%）"
