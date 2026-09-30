"""集中配置：路径、颜色常量与业务状态枚举。

所有数据均为虚构，仅用于演示支票簿领用与作废核验流程。
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "bank.db"
VOUCHER_DIR = DATA_DIR / "vouchers"

# 页面配色：纸张白、银行深蓝、印章红
COLOR_PAPER = "#faf6ec"
COLOR_PAPER_DEEP = "#f3ecdc"
COLOR_INK = "#1c2430"
COLOR_BLUE = "#0b2e59"
COLOR_BLUE_SOFT = "#2f5d8f"
COLOR_RED = "#b3261e"
COLOR_RED_SOFT = "#d98a84"
COLOR_GREEN = "#1f6b45"

# 支票号码状态（同一号码任一时刻只能属于一个有效状态）
PAGE_AVAILABLE = "available"    # 空白库存
PAGE_RESERVED = "reserved"      # 已按连续号段预留
PAGE_ISSUED = "issued"          # 双人出库，待客户签收
PAGE_ACTIVE = "active"          # 客户签收并启用
PAGE_STOPPED = "stopped"        # 挂失止付冻结（未使用号码）
PAGE_VOID = "void"              # 作废打孔
PAGE_DESTROYED = "destroyed"    # 已销毁（终态，不可逆）

PAGE_STATUS_LABEL = {
    PAGE_AVAILABLE: "在库空白",
    PAGE_RESERVED: "已预留",
    PAGE_ISSUED: "已出库待签收",
    PAGE_ACTIVE: "已签收启用",
    PAGE_STOPPED: "止付冻结",
    PAGE_VOID: "作废打孔",
    PAGE_DESTROYED: "已销毁",
}

# 申请状态
APP_PENDING = "pending"
APP_APPROVED = "approved"
APP_REJECTED = "rejected"

# 支票簿状态
BK_RESERVED = "reserved"
BK_ISSUED = "issued"
BK_ACTIVE = "active"
BK_CLAIM_FAILED = "claim_failed"   # 签收失败（可重试签收）
BK_REFUSED = "refused"             # 客户拒领
BK_STOPPED = "stopped"
BK_CLOSED = "closed"               # 销户交回处理完成
BK_DESTROYED = "destroyed"

BOOKLET_STATUS_LABEL = {
    BK_RESERVED: "已预留待出库",
    BK_ISSUED: "已出库待签收",
    BK_ACTIVE: "已签收启用",
    BK_CLAIM_FAILED: "签收失败待处理",
    BK_REFUSED: "客户拒领",
    BK_STOPPED: "挂失止付",
    BK_CLOSED: "销户已交回",
    BK_DESTROYED: "已销毁",
}

# 销毁批次
BATCH_OPEN = "open"
BATCH_DISCREPANCY = "discrepancy"
BATCH_CONFIRMED = "confirmed"
