"""HTML 视图：纸张白 / 银行深蓝 / 印章红 的纸质台账风格。

号码段一律以“起止号 + 缺口”压缩成连续条带，不逐张画卡片；
作废与销毁同时给出文字、水印式小标记与时间。
"""
from __future__ import annotations

import html
from urllib.parse import quote

from . import config

NAV = [
    ("/", "总览"),
    ("/applications", "申请审核"),
    ("/inventory", "号码段库存"),
    ("/disburse", "双人出库"),
    ("/signoff", "签收启用"),
    ("/stop", "挂失止付"),
    ("/returns", "空白页交回"),
    ("/destroy", "销毁台账"),
    ("/ledger", "事件台账"),
]

STATUS_COLOR = {
    config.PAGE_AVAILABLE: "#c9d4e2",
    config.PAGE_RESERVED: "#7fa3c9",
    config.PAGE_ISSUED: "#c9a24b",
    config.PAGE_ACTIVE: config.COLOR_GREEN,
    config.PAGE_STOPPED: config.COLOR_RED,
    config.PAGE_VOID: "#8a8a8a",
    config.PAGE_DESTROYED: "#3a3a3a",
}


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def fmt_no(n: int) -> str:
    return f"{n:08d}"


def layout(title: str, body: str, msg: str = "") -> str:
    nav_html = "".join(
        f'<a href="{u}"><span class="nav-seal">建</span>{esc(t)}</a>' for u, t in NAV
    )
    flash = f'<div class="flash">{esc(msg)}</div>' if msg else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 支票簿领用与作废核验系统</title>
<style>
:root {{
  --paper: {config.COLOR_PAPER}; --paper2: {config.COLOR_PAPER_DEEP};
  --ink: {config.COLOR_INK}; --blue: {config.COLOR_BLUE};
  --blue2: {config.COLOR_BLUE_SOFT}; --red: {config.COLOR_RED};
  --red2: {config.COLOR_RED_SOFT}; --green: {config.COLOR_GREEN};
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--paper); color:var(--ink);
  font-family:"Noto Serif CJK SC","Songti SC","SimSun",serif; line-height:1.55; }}
header {{ background:var(--blue); color:#f3ecd9; padding:14px 26px;
  border-bottom:4px double var(--red); }}
header h1 {{ margin:0; font-size:19px; letter-spacing:2px; }}
header .sub {{ font-size:12px; opacity:.82; margin-top:3px; }}
nav {{ background:#09264a; padding:0 18px; display:flex; flex-wrap:wrap; gap:2px; }}
nav a {{ color:#e9e2cf; text-decoration:none; padding:9px 13px; font-size:13.5px;
  border-bottom:3px solid transparent; }}
nav a:hover {{ background:#133c6e; border-bottom-color:var(--red); }}
.nav-seal {{ display:inline-block; background:var(--red); color:#fff; border-radius:3px;
  width:18px; height:18px; line-height:18px; text-align:center; font-size:12px; margin-right:5px; }}
main {{ max-width:1180px; margin:18px auto 60px; padding:0 20px; }}
h2 {{ color:var(--blue); border-left:6px solid var(--red); padding-left:10px;
  font-size:17px; margin:26px 0 12px; }}
.card {{ background:#fffdf6; border:1px solid #d9cfb8; border-radius:4px;
  padding:14px 16px; margin-bottom:16px; box-shadow:0 1px 0 #e4dccb; }}
.grid {{ display:grid; gap:12px; }}
.grid-4 {{ grid-template-columns:repeat(4,1fr); }}
.grid-3 {{ grid-template-columns:repeat(3,1fr); }}
@media (max-width:900px) {{ .grid-4,.grid-3 {{ grid-template-columns:1fr 1fr; }} }}
.stat {{ background:#fffdf6; border:1px solid #d9cfb8; border-top:4px solid var(--blue);
  padding:10px 14px; border-radius:3px; }}
.stat .n {{ font-size:26px; font-weight:700; color:var(--blue); }}
.stat .l {{ font-size:12.5px; color:#5a5346; }}
.stat.red {{ border-top-color:var(--red); }} .stat.red .n {{ color:var(--red); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; background:#fffdf6; }}
th,td {{ border:1px solid #d9cfb8; padding:7px 9px; text-align:left; vertical-align:middle; }}
th {{ background:var(--blue); color:#f3ecd9; font-weight:600; letter-spacing:1px; }}
tr:nth-child(even) td {{ background:#fbf5e6; }}
form.inline {{ display:inline; }}
select,input[type=text],input[type=number],input[type=search] {{ font:inherit;
  border:1px solid #b8ab8c; border-radius:3px; padding:4px 6px; background:#fff; }}
button,.btn {{ font:inherit; cursor:pointer; border:1px solid var(--blue);
  background:var(--blue); color:#f3ecd9; padding:4px 12px; border-radius:3px; }}
button:hover {{ background:#13437e; }}
button.danger {{ border-color:var(--red); background:var(--red); }}
button.ghost {{ background:transparent; color:var(--blue); }}
.flash {{ background:#f7e3e1; border:1px solid var(--red); color:var(--red);
  padding:9px 14px; border-radius:3px; margin:12px 0; font-size:13.5px; }}
.ok-tag {{ color:var(--green); font-weight:700; }}
.bad-tag {{ color:var(--red); font-weight:700; }}
.muted {{ color:#857c69; font-size:12px; }}
a.vlink {{ color:var(--blue2); }}

/* 连续号码条带 */
.strip {{ display:flex; width:100%; height:30px; border:1px solid #b8ab8c;
  border-radius:3px; overflow:hidden; background:var(--paper2); }}
.seg {{ display:flex; align-items:center; justify-content:center; color:#fff;
  font-size:11px; white-space:nowrap; overflow:hidden; border-right:1px solid rgba(255,255,255,.55);
  min-width:0; }}
.seg.gap {{ background:repeating-linear-gradient(45deg,#fff 0 5px,var(--red2) 5px 10px);
  color:var(--red); border-right:1px dashed var(--red); }}
.legend {{ display:flex; flex-wrap:wrap; gap:10px; font-size:12px; margin:6px 0 0; }}
.legend i {{ display:inline-block; width:12px; height:12px; border:1px solid #777;
  margin-right:4px; vertical-align:-2px; }}

/* 水印式小标记 */
.wm {{ position:relative; display:inline-flex; align-items:center; gap:6px; }}
.wm b {{ display:inline-block; border:2px solid rgba(179,38,30,.55); color:rgba(179,38,30,.6);
  transform:rotate(-12deg); font-size:11px; padding:0 5px; border-radius:3px;
  font-weight:700; letter-spacing:2px; background:rgba(179,38,30,.05); }}
.wm.ink b {{ border-color:rgba(40,40,40,.55); color:rgba(40,40,40,.62); background:rgba(0,0,0,.04); }}
.three-col th {{ text-align:center; }}
.three-col td {{ vertical-align:top; }}
.evlog td {{ font-size:12.5px; }}
.evlog .cat {{ white-space:nowrap; color:var(--blue); font-weight:600; }}
pre.voucher {{ white-space:pre-wrap; font-family:inherit; background:#fffdf6;
  border:1px solid #d9cfb8; padding:18px 22px; border-radius:3px; line-height:1.9; }}
.seal-line {{ color:var(--red); font-weight:700; }}
.foot {{ text-align:center; color:#9a917e; font-size:12px; margin:30px 0 0; }}
</style></head>
<body>
<header>
  <h1>中国建设银行 · 支票簿领用与作废核验系统 <span style="color:var(--red2);font-size:12px;">（虚构演示环境）</span></h1>
  <div class="sub">全部数据均为虚构，仅用于流程演示 · SQLite 号码状态台账 · 本地凭证目录 · 进程内扫描</div>
</header>
<nav>{nav_html}</nav>
<main>{flash}{body}
<div class="foot">号码唯一 · 经办复核分离 · 止付阻断 · 销毁不可逆 · 双人监销 —— 纸张白 / 银行深蓝 / 印章红</div>
</main></body></html>"""


# ---------------------------------------------------------------- 号码条带

def number_strip(status_map: dict, lo: int, hi: int, gap_set: set | None = None) -> str:
    """把 lo..hi 渲染为按状态着色的连续条带；缺页（gap_set 或无状态）为红斜纹缺口。"""
    gap_set = gap_set or set()
    bands: list[tuple] = []  # (kind, start, end)
    kind = None
    s = lo
    prev = lo - 1
    for n in range(lo, hi + 1):
        k = "gap" if n in gap_set or n not in status_map else status_map[n]
        if kind is None:
            kind, s = k, n
        elif k != kind:
            bands.append((kind, s, prev))
            kind, s = k, n
        prev = n
    if kind is not None:
        bands.append((kind, s, prev))

    total = hi - lo + 1
    parts = []
    for k, a, b in bands:
        pct = (b - a + 1) / total * 100
        label = "缺页" if k == "gap" else (f"{fmt_no(a)}–{fmt_no(b)}" if a != b else fmt_no(a))
        cnt = f"×{b - a + 1}"
        if k == "gap":
            style = ""
            title = f"物理缺页 {fmt_no(a)}–{fmt_no(b)}（{b - a + 1} 张）"
        else:
            color = STATUS_COLOR.get(k, "#999")
            style = f' style="flex:0 0 {pct:.3f}%;background:{color}"'
            title = f"{config.PAGE_STATUS_LABEL.get(k, k)} {fmt_no(a)}–{fmt_no(b)}（{b - a + 1} 张）"
        if k == "gap":
            style = f' style="flex:0 0 {pct:.3f}%"'
        parts.append(
            f'<div class="seg {k}"{style} title="{esc(title)}">{esc(label)}<span class="muted" style="color:inherit;opacity:.85">{cnt}</span></div>'
        )
    return f'<div class="strip">{"".join(parts)}</div>'


def strip_legend() -> str:
    items = [(config.PAGE_STATUS_LABEL[s], STATUS_COLOR[s]) for s in (
        config.PAGE_AVAILABLE, config.PAGE_RESERVED, config.PAGE_ISSUED,
        config.PAGE_ACTIVE, config.PAGE_STOPPED, config.PAGE_VOID, config.PAGE_DESTROYED)]
    items.append(("物理缺页", "repeating-linear-gradient(45deg,#fff 0 5px,#d98a84 5px 10px)"))
    return '<div class="legend">' + "".join(
        f'<span><i style="background:{c}"></i>{esc(l)}</span>' for l, c in items
    ) + "</div>"


def wm_mark(status: str, ts: str = "") -> str:
    """作废/销毁：文字 + 水印式小标记 + 时间，三者同时呈现。"""
    if status == config.PAGE_VOID:
        return (f'<span class="wm"><span>作废打孔</span><b>作廢</b>'
                f'<span class="muted">{esc(ts or "")}</span></span>')
    if status == config.PAGE_DESTROYED:
        return (f'<span class="wm ink"><span>已销毁·终态</span><b>毀銷</b>'
                f'<span class="muted">{esc(ts or "")}</span></span>')
    return esc(config.PAGE_STATUS_LABEL.get(status, status))


def page_link(path: str, text: str) -> str:
    return f'<a class="vlink" href="/voucher?path={quote(path)}">{esc(text)}</a>'
