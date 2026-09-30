# -*- coding: utf-8 -*-
"""支票簿系统 Web 界面（Python 标准库 http.server，零第三方依赖）。

页面：
  /            总览台账
  /apply       申请与审核
  /stock       号码段库存（连续条带 + 缺口）
  /release     双人出库
  /signoff     客户签收 / 拒领
  /books/<id>  账册详情：号码条带、止付、交回、打孔
  /destroy     销毁批次台账
  /scans       进程内扫描记录
配色：纸张白 #fbf8f1、银行深蓝 #0b2a5b、印章红 #b3202e。
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db as dbmod
from .adapters import AccountAdapter, SignatureAdapter
from .scanner import ScanWorker
from .service import CheckbookService
from .db import (
    BusinessError,
    BK_DRAFT, BK_RESERVED, BK_RELEASED, BK_ACTIVE, BK_STOP_PAY,
    BK_PART_VOID, BK_FULLY_VOID, BK_DESTROYED, BK_REJECTED,
    ST_RESERVED, ST_RELEASED, ST_ACTIVE, ST_USED, ST_STOP,
    ST_VOID, ST_DESTROYED, ST_REJECTED,
    DEST_OPEN, DEST_CONFIRMED, DEST_DESTROYED,
)

# --------------------------------------------------------------------- #
# 展示元数据
# --------------------------------------------------------------------- #
BOOK_META = {
    BK_DRAFT:       ("待审核", "stamp-blue"),
    BK_RESERVED:    ("已预留·待出库", "stamp-blue"),
    BK_RELEASED:    ("已出库·待签收", "stamp-blue"),
    BK_ACTIVE:      ("已启用", "stamp-blue solid"),
    BK_STOP_PAY:    ("挂失止付", "stamp-red"),
    BK_PART_VOID:   ("部分作废·缺页", "stamp-red"),
    BK_FULLY_VOID:  ("全部作废·待销毁", "stamp-red"),
    BK_DESTROYED:   ("已销毁", "stamp-red solid"),
    BK_REJECTED:    ("已驳回/拒领", "stamp-grey"),
}
NUM_META = {
    ST_RESERVED:  ("预留", "seg-reserved"),
    ST_RELEASED:  ("出库", "seg-released"),
    ST_ACTIVE:    ("启用", "seg-active"),
    ST_USED:      ("已用", "seg-used"),
    ST_STOP:      ("止付", "seg-stop"),
    ST_VOID:      ("作废", "seg-void"),
    ST_DESTROYED: ("销毁", "seg-destroyed"),
    ST_REJECTED:  ("释放", "seg-rejected"),
}
TELLERS = ["T001 张明", "T002 王芳", "T003 李强"]


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def no8(n: int) -> str:
    return f"{int(n):08d}"


# --------------------------------------------------------------------- #
# 条带渲染：连续同状态号码合并成一段，缺口单独成段
# --------------------------------------------------------------------- #
def number_band(rows, start: int | None = None, end: int | None = None,
                scale_note: bool = True) -> str:
    """rows: check_numbers 行（check_no,status）。返回连续条带 HTML。"""
    if not rows:
        return '<div class="empty">（无号码）</div>'
    lo = start if start is not None else rows[0]["check_no"]
    hi = end if end is not None else rows[-1]["check_no"]
    status_by_no = {r["check_no"]: r["status"] for r in rows}

    runs: list[tuple[int, int, str | None]] = []  # start,end,status(None=缺口)
    cur_s = lo
    cur_status = status_by_no.get(lo)  # None 即以缺口开头
    for n in range(lo + 1, hi + 1):
        st = status_by_no.get(n)
        if st != cur_status:
            runs.append((cur_s, n - 1, cur_status))
            cur_s, cur_status = n, st
    runs.append((cur_s, hi, cur_status))

    total = hi - lo + 1
    parts = ['<div class="band" role="img" aria-label="号码状态条带">']
    for a, b, st in runs:
        width = max(2.0, (b - a + 1) / total * 100)
        label = f"{no8(a)}–{no8(b)}" if a != b else no8(a)
        if st is None:
            parts.append(
                f'<div class="seg seg-gap" style="flex:{b-a+1} {b-a+1} 0" '
                f'title="缺页 {label}（库存断号）"><span>缺页 {label}</span></div>')
        else:
            text, cls = NUM_META[st]
            parts.append(
                f'<div class="seg {cls}" style="flex:{b-a+1} {b-a+1} 0" '
                f'title="{text} {label}（{b-a+1} 张）">'
                f"<span>{label}</span><i>{text}×{b-a+1}</i></div>")
    parts.append("</div>")
    return "".join(parts)


def stock_band(conn, branch: str) -> str:
    """网点库存全量条带：库存区间内逐号着色，区间之间为缺口。"""
    ranges = conn.execute(
        "SELECT start_no,end_no FROM stock_ranges WHERE branch_code=?"
        " ORDER BY start_no", (branch,)).fetchall()
    if not ranges:
        return '<div class="empty">（该网点尚未登记号码库存）</div>'
    lo, hi = ranges[0]["start_no"], ranges[-1]["end_no"]
    rows = conn.execute(
        "SELECT check_no,status FROM check_numbers WHERE branch_code=? "
        "AND check_no BETWEEN ? AND ?", (branch, lo, hi)).fetchall()
    used = {r["check_no"]: r["status"] for r in rows}
    in_stock = set()
    for rg in ranges:
        in_stock.update(range(rg["start_no"], rg["end_no"] + 1))
    # 库存断号渲染为缺口；已占用号按真实状态着色
    class _R:
        def __init__(self, n, st):
            self.check_no, self.status = n, st

        def __getitem__(self, k):
            return getattr(self, k)

    fake_rows = [_R(n, used.get(n))
                 for n in range(lo, hi + 1) if n in in_stock]
    return number_band(fake_rows, lo, hi)


def stamp(status: str) -> str:
    text, cls = BOOK_META.get(status, (status, "stamp-grey"))
    return f'<span class="stamp {cls}">{esc(text)}</span>'


# --------------------------------------------------------------------- #
# 页面骨架
# --------------------------------------------------------------------- #
CSS = """
:root{--paper:#fbf8f1;--paper2:#fffdf8;--ink:#1c2430;--blue:#0b2a5b;
--blue2:#1f4e96;--blue3:#dce6f5;--red:#b3202e;--red2:#f3dcd9;
--grey:#7a818c;--line:#d8d2c4;}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font-family:"Songti SC","Noto Serif SC","SimSun",serif;font-size:14px}
a{color:var(--blue2)}
header{background:var(--blue);color:#fff;padding:14px 26px;
display:flex;align-items:baseline;gap:18px;flex-wrap:wrap}
header h1{font-size:19px;margin:0;letter-spacing:2px}
header .sub{opacity:.75;font-size:12px}
nav{background:#09224a;padding:0 26px;display:flex;gap:4px;flex-wrap:wrap}
nav a{color:#cdd9ee;text-decoration:none;padding:9px 14px;font-size:13px;
border-bottom:2px solid transparent}
nav a:hover{color:#fff;background:#12315f}
nav a.on{color:#fff;border-bottom-color:var(--red)}
main{max-width:1180px;margin:22px auto;padding:0 18px}
.card{background:var(--paper2);border:1px solid var(--line);
border-radius:4px;padding:16px 18px;margin:0 0 18px;
box-shadow:0 1px 3px rgba(20,30,50,.06)}
.card h2{margin:0 0 12px;font-size:16px;color:var(--blue);
border-left:4px solid var(--red);padding-left:10px}
.card h3{font-size:14px;color:var(--blue);margin:14px 0 8px}
table{border-collapse:collapse;width:100%;background:var(--paper2)}
th,td{border:1px solid var(--line);padding:7px 9px;text-align:left;
vertical-align:middle;font-size:13px}
th{background:#eef2f8;color:var(--blue);font-weight:600}
tr:nth-child(even) td{background:#faf7f0}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
.kpi{flex:1 1 130px;background:var(--paper2);border:1px solid var(--line);
border-top:3px solid var(--blue);padding:10px 14px;border-radius:3px}
.kpi .v{font-size:24px;color:var(--blue);font-weight:700}
.kpi.warn{border-top-color:var(--red)}.kpi.warn .v{color:var(--red)}
.kpi .l{font-size:12px;color:var(--grey)}
.btn{display:inline-block;background:var(--blue);color:#fff;border:0;
padding:6px 14px;border-radius:3px;cursor:pointer;font-size:13px;
font-family:inherit;text-decoration:none}
.btn:hover{background:var(--blue2)}
.btn.red{background:var(--red)}.btn.red:hover{background:#921924}
.btn.ghost{background:#fff;color:var(--blue);border:1px solid var(--blue)}
.btn.mini{padding:3px 9px;font-size:12px}
input,select,textarea{font-family:inherit;font-size:13px;padding:5px 8px;
border:1px solid #b9c0cb;border-radius:3px;background:#fff}
label.fl{display:inline-flex;align-items:center;gap:6px;margin:3px 12px 3px 0}
.flash{background:var(--red2);border:1px solid var(--red);color:#7a1620;
padding:9px 14px;border-radius:3px;margin-bottom:14px}
.ok{background:#e3f0e6;border-color:#3a7d4d;color:#1f5130}
.stamp{display:inline-block;padding:2px 10px;border:2px solid var(--blue);
color:var(--blue);border-radius:4px;font-size:12px;font-weight:700;
transform:rotate(-6deg);letter-spacing:3px;background:rgba(255,255,255,.65)}
.stamp.red,.stamp-red{border-color:var(--red);color:var(--red)}
.stamp.solid{background:var(--blue);color:#fff}
.stamp-red{border-color:var(--red)!important;color:var(--red)!important}
.stamp-red.solid{background:var(--red);color:#fff}
.stamp-grey{border-color:var(--grey);color:var(--grey)}
.muted{color:var(--grey);font-size:12px}
.band{display:flex;width:100%;height:58px;border:1px solid #9aa3b0;
border-radius:3px;overflow:hidden;background:#f3efe6}
.seg{display:flex;flex-direction:column;justify-content:center;
align-items:center;min-width:0;border-right:1px solid rgba(255,255,255,.75);
font-size:10px;overflow:hidden;white-space:nowrap}
.seg span{font-weight:700;font-variant-numeric:tabular-nums}
.seg i{font-style:normal;opacity:.85;font-size:9px}
.seg-reserved{background:#c9d6ec;color:#1d3c74}
.seg-released{background:var(--blue2);color:#fff}
.seg-active{background:var(--blue);color:#fff}
.seg-used{background: repeating-linear-gradient(45deg,#9aa1ab,#9aa1ab 4px,#8b929c 4px,#8b929c 8px);color:#fff}
.seg-stop{background:var(--red2);color:var(--red);font-weight:700}
.seg-void{background:#fff;color:var(--red);border:2px dashed var(--red);
border-right:2px dashed var(--red);background-image:radial-gradient(circle at 50% 50%,transparent 3px,rgba(179,32,46,.25) 3.5px)}
.seg-destroyed{background:var(--red);color:#fff}
.seg-destroyed i::after{content:" ●"}
.seg-rejected{background:#e7e4dc;color:var(--grey)}
.seg-gap{background:repeating-linear-gradient(45deg,#e4dfd2,#e4dfd2 6px,#d7d1c2 6px,#d7d1c2 12px);
color:#8a7f66;border-right:1px dashed #b3a98f;justify-content:center}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin:8px 0 4px;font-size:12px}
.legend i{display:inline-block;width:14px;height:14px;border-radius:2px;
vertical-align:-2px;margin-right:4px;border:1px solid rgba(0,0,0,.15)}
.duo{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;
border:1px solid var(--line)}
.duo>div{padding:10px 12px;border-right:1px solid var(--line)}
.duo>div:last-child{border-right:0}
.duo h4{margin:0 0 6px;color:var(--blue);font-size:12px}
.log{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.log td{white-space:pre-wrap}
.watermark{position:relative;overflow:hidden}
.watermark::after{content:attr(data-mark);position:absolute;right:8px;top:50%;
transform:translateY(-50%) rotate(-12deg);font-size:34px;font-weight:800;
color:rgba(179,32,46,.10);letter-spacing:8px;pointer-events:none}
.rowline{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end}
.pill{font-size:11px;padding:1px 7px;border-radius:9px;background:var(--blue3);
color:var(--blue)}
.pill.red{background:var(--red2);color:var(--red)}
.empty{color:var(--grey);padding:14px;text-align:center}
footer{color:var(--grey);text-align:center;font-size:12px;padding:20px}
"""

JS = ""


def layout(body: str, title: str, on: str = "", msg: str = "", ok: bool = False,
           branch: str = "BJ001") -> bytes:
    nav = [
        ("/", "总览台账", ""),
        ("/apply", "申请审核", "apply"),
        ("/stock", "号码段库存", "stock"),
        ("/release", "双人出库", "release"),
        ("/signoff", "签收启用", "signoff"),
        ("/destroy", "销毁台账", "destroy"),
        ("/scans", "扫描记录", "scans"),
    ]
    links = "".join(
        f'<a href="{u}?branch={branch}" class="{"on" if k == on else ""}">{t}</a>'
        for u, t, k in nav)
    flash = (f'<div class="flash {"ok" if ok else ""}">{esc(msg)}</div>'
             if msg else "")
    page = f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 支票簿核验系统</title><style>{CSS}</style></head>
<body>
<header><h1>中国建设银行 · 支票簿领用与作废核验系统</h1>
<span class="sub">网点 {esc(branch)} ｜ 全部数据均为虚构演示，非真实金融凭证</span></header>
<nav>{links}</nav>
<main>{flash}{body}</main>
<footer>虚构数据演示系统 · SQLite 单库 · 凭证仅存本地目录 · © 教学示例</footer>
</body></html>"""
    return page.encode("utf-8")


# --------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------- #
class App:
    def __init__(self, db_path: str, voucher_root: str):
        self.db_path = db_path
        self.vroot = Path(voucher_root).resolve()

    def service(self, conn):
        return CheckbookService(conn, self.vroot,
                                AccountAdapter(conn), SignatureAdapter())


class Handler(BaseHTTPRequestHandler):
    app: App = None  # type: ignore

    def log_message(self, fmt, *args):
        sys.stderr.write("[web] " + fmt % args + "\n")

    # ---- 工具 ----
    def _qs(self):
        u = urllib.parse.urlparse(self.path)
        return u.path, urllib.parse.parse_qs(u.query)

    def _branch(self, qs, form=None):
        b = (form.get("branch") if form else None) or qs.get("branch", ["BJ001"])[0]
        return b

    def _send(self, body: bytes, status: int = 200, ctype="text/html; charset=utf-8",
              extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, url: str):
        self._send(b"", 303, extra={"Location": url})

    def _conn(self):
        return dbmod.connect(self.app.db_path)

    def _form(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _page(self, conn, qs, renderer):
        try:
            return renderer(conn, qs)
        except BusinessError as e:
            return layout(f'<div class="flash">{esc(e)}</div>', "业务异常")

    # ---- 路由 ----
    def do_GET(self):
        path, qs = self._qs()
        conn = self._conn()
        try:
            if path == "/":
                self._send(self._page(conn, qs, self.page_dashboard))
            elif path == "/apply":
                self._send(self._page(conn, qs, self.page_apply))
            elif path == "/stock":
                self._send(self._page(conn, qs, self.page_stock))
            elif path == "/release":
                self._send(self._page(conn, qs, self.page_release))
            elif path == "/signoff":
                self._send(self._page(conn, qs, self.page_signoff))
            elif path == "/destroy":
                self._send(self._page(conn, qs, self.page_destroy))
            elif path == "/scans":
                self._send(self._page(conn, qs, self.page_scans))
            elif re.fullmatch(r"/books/\d+", path):
                bid = int(path.rsplit("/", 1)[-1])
                self._send(self._page(conn, {**qs, "bid": [str(bid)]},
                                      self.page_book))
            elif path == "/voucher":
                self.serve_voucher(qs)
            else:
                self._send(layout("<div class='flash'>页面不存在</div>", "404"), 404)
        finally:
            conn.close()

    def do_POST(self):
        path, qs = self._qs()
        form = self._form()
        conn = self._conn()
        svc = self.app.service(conn)
        branch = self._branch(qs, form)
        try:
            back = self.dispatch(svc, path, form, branch)
            if back is None:
                back = f"/?branch={branch}"
            self._redirect(back)
        except BusinessError as e:
            self._send(layout(
                f'<div class="flash">{esc(e)}</div>'
                f'<p><a class="btn ghost" href="javascript:history.back()">返回重试</a></p>',
                "操作被拒绝"), 400)
        finally:
            conn.close()

    def dispatch(self, svc: CheckbookService, path: str, f: dict,
                 branch: str) -> str | None:
        def fid(name):
            return int(f.get(name, "0") or 0)

        if path == "/apply":
            book = svc.apply(branch, f["acct_no"], int(f["count"]), f["teller"])
            return f"/apply?branch={branch}&msg=" + urllib.parse.quote(
                f"申请 {book['apply_ref']} 已提交，资格核验通过，等待审核")
        m = re.fullmatch(r"/books/(\d+)/(approve|reject|release|sign|refuse|"
                         r"stop|used|handover|void-missing)", path)
        if m:
            bid, action = int(m.group(1)), m.group(2)
            if action == "approve":
                svc.approve(bid, f["reviewer"])
                msg = "审核通过，连续号码段已预留"
            elif action == "reject":
                svc.reject(bid, f["reviewer"], f.get("reason", ""))
                msg = "申请已驳回，号码占位释放"
            elif action == "release":
                svc.release(bid, f["teller"], f["reviewer"])
                msg = "双人出库完成，等待客户签收"
            elif action == "sign":
                svc.sign(bid, force_pass=("force" in f) or None)
                msg = "签名核对通过，客户签收并正式启用"
            elif action == "refuse":
                svc.refuse(bid, f["teller"], f.get("reason", ""))
                msg = "已登记客户拒领，号码段冻结止付"
            elif action == "stop":
                n = svc.stop_payment(bid, f["teller"], f.get("reason", ""))
                msg = f"挂失止付完成，冻结未使用号码 {n} 张"
            elif action == "used":
                svc.mark_used(bid, fid("check_no"))
                msg = f"号码 {fid('check_no'):08d} 已登记为客户使用"
            elif action == "handover":
                nos = sorted({int(x) for x in re.findall(r"\d+", f.get("nos", ""))})
                svc.handover(bid, nos, f["teller"], f["reviewer"])
                msg = "空白页交回清点完成，已逐张作废打孔（详见账册）"
            elif action == "void-missing":
                svc.void_missing(bid, fid("check_no"), f["teller"])
                msg = f"缺页 {fid('check_no'):08d} 已补作废打孔"
            return f"/books/{bid}?branch={branch}&msg=" + urllib.parse.quote(msg)

        if path == "/batches/create":
            b = svc.create_batch(branch)
            return f"/destroy?branch={branch}&msg=" + urllib.parse.quote(
                f"销毁批次 {b['batch_no']} 已建批")
        m = re.fullmatch(r"/batches/(\d+)/(add|confirm|destroy)", path)
        if m:
            batch_id, action = int(m.group(1)), m.group(2)
            if action == "add":
                n = svc.add_to_batch(batch_id, fid("book_id"))
                msg = f"已归集 {n} 张作废号码，号码即刻置为销毁（不可逆）"
            elif action == "confirm":
                svc.confirm_batch(batch_id, f["confirm_a"], f["confirm_b"])
                msg = "双人确认封存完成，等待实物销毁"
            else:
                svc.destroy_batch(batch_id, f["confirm_a"], f["confirm_b"],
                                  int(f.get("actual_count", "0")),
                                  f.get("diff_note", ""))
                msg = "实物销毁入账，台账封存"
            return f"/destroy?branch={branch}&msg=" + urllib.parse.quote(msg)
        if path == "/scans/run":
            worker.run_once()
            return f"/scans?branch={branch}&msg=" + urllib.parse.quote(
                "已立即执行一轮扫描")
        raise BusinessError(f"未知操作：{path}")

    # ---- 凭证下载 ----
    def serve_voucher(self, qs):
        rel = qs.get("path", [""])[0]
        p = (self.app.vroot / rel).resolve()
        if self.app.vroot not in p.parents or not p.is_file():
            self._send(b"voucher not found", 404, "text/plain; charset=utf-8")
            return
        self._send(p.read_bytes(), 200, "text/plain; charset=utf-8")

    # ---- 小组件 ----
    def _teller_opts(self, name: str, default: str = "T001"):
        return (f'<select name="{name}">' + "".join(
            f'<option value="{t.split()[0]}"'
            f'{" selected" if t.startswith(default) else ""}>{esc(t)}</option>'
            for t in TELLERS) + "</select>")

    def _flash_from_qs(self, qs) -> tuple[str, bool]:
        msg = qs.get("msg", [""])[0]
        return msg, msg != ""

    # ---- 页面：总览 ----
    def page_dashboard(self, conn, qs):
        branch = qs.get("branch", ["BJ001"])[0]
        rows = conn.execute(
            "SELECT * FROM checkbooks WHERE branch_code=? ORDER BY id",
            (branch,)).fetchall()
        counts = {s: 0 for s in BOOK_META}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        kpis = [
            ("申请账册总数", len(rows), ""),
            ("待审核", counts[BK_DRAFT], ""),
            ("待出库", counts[BK_RESERVED], ""),
            ("待签收", counts[BK_RELEASED], ""),
            ("已启用", counts[BK_ACTIVE], ""),
            ("止付/拒领", counts[BK_STOP_PAY], "warn"),
            ("缺页部分作废", counts[BK_PART_VOID], "warn"),
            ("待销毁", counts[BK_FULLY_VOID], "warn"),
            ("已销毁", counts[BK_DESTROYED], "warn"),
        ]
        kpi_html = "".join(
            f'<div class="kpi {c}"><div class="v">{v}</div>'
            f'<div class="l">{esc(t)}</div></div>' for t, v, c in kpis)
        trs = []
        for r in rows:
            trs.append(
                "<tr>"
                f'<td><a href="/books/{r["id"]}?branch={branch}">{esc(r["apply_ref"])}</a></td>'
                f'<td>{esc(r["acct_no"])}<br><span class="muted">{esc(r["cust_name"])}</span></td>'
                f'<td class="log">{no8(r["start_no"])}–{no8(r["end_no"])}'
                f'<br><span class="muted">{r["end_no"]-r["start_no"]+1} 张</span></td>'
                f"<td>{stamp(r['status'])}</td>"
                f'<td class="muted">{self._book_time(r)}</td>'
                "</tr>")
        body = f"""
<div class="card"><h2>状态总览</h2><div class="kpis">{kpi_html}</div></div>
<div class="card"><h2>支票簿台账（{esc(branch)}）</h2>
<table><thead><tr><th>申请编号</th><th>账户 / 户名</th><th>号码段</th>
<th>状态（印章标记）</th><th>最近时间</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan=5 class=empty>暂无申请</td></tr>'}</tbody></table></div>"""
        msg, show = self._flash_from_qs(qs)
        return layout(body, "总览", "", msg if show else "", True, branch)

    @staticmethod
    def _book_time(r) -> str:
        for t, label in ((r["destroyed_at"] if "destroyed_at" in r.keys() else None, "销毁"),
                         (r["signed_at"], "签收"), (r["stop_at"], "止付"),
                         (r["released_at"], "出库"), (r["created_at"], "申请")):
            if t:
                return f"{label} {t}"
        return ""

    # ---- 页面：申请审核 ----
    def page_apply(self, conn, qs):
        branch = qs.get("branch", ["BJ001"])[0]
        svc = self.app.service(conn)
        drafts = conn.execute(
            "SELECT * FROM checkbooks WHERE branch_code=? AND status=? ORDER BY id",
            (branch, BK_DRAFT)).fetchall()
        drows = []
        for r in drafts:
            elig = svc.accts.check(branch, r["acct_no"])
            drows.append(f"""<tr><td>{esc(r['apply_ref'])}</td>
<td>{esc(r['acct_no'])} {esc(r['cust_name'])}</td>
<td class=log>{no8(r['start_no'])}–{no8(r['end_no'])}</td>
<td>经办 {esc(r['teller_id'])}<br><span class="muted">{elig.ref}：{esc(elig.reason)}</span></td>
<td><form method=post action="/books/{r['id']}/approve?branch={branch}"
 style="display:flex;gap:6px;align-items:center">
{self._teller_opts('reviewer','T002')}<button class="btn mini">通过</button></form>
<form method=post action="/books/{r['id']}/reject?branch={branch}"
 style="display:flex;gap:6px;margin-top:4px">
{self._teller_opts('reviewer','T002')}
<input name=reason placeholder="驳回原因" style="width:130px">
<button class="btn red mini">驳回</button></form></td></tr>""")
        custs = conn.execute(
            "SELECT acct_no,cust_name FROM customers WHERE branch_code=? ORDER BY acct_no",
            (branch,)).fetchall()
        opts = "".join(f'<option value="{esc(r["acct_no"])}">'
                       f'{esc(r["acct_no"])} {esc(r["cust_name"])}</option>'
                       for r in custs)
        body = f"""
<div class="card"><h2>新建支票簿申请</h2>
<form method=post action="/apply?branch={branch}" class="rowline">
<label class=fl>账户代号<select name=acct_no>{opts}</select></label>
<label class=fl>张数<input name=count value=5 type=number min=1 max=50
 style="width:70px"></label>
<label class=fl>经办柜员{self._teller_opts('teller','T001')}</label>
<button class="btn">提交申请并核验资格</button>
<span class=muted>尾号 91 账户资格不通过，可用于演示；申请通过后按连续号码段预留</span>
</form></div>
<div class="card"><h2>待审核申请（{len(drows)}）</h2>
<table><thead><tr><th>申请编号</th><th>账户/户名</th><th>预留号码段</th>
<th>经办 / 资格核验</th><th>审核（须异于经办）</th></tr></thead>
<tbody>{''.join(drows) or '<tr><td colspan=5 class=empty>无待审核申请</td></tr>'}</tbody></table></div>"""
        msg, show = self._flash_from_qs(qs)
        return layout(body, "申请审核", "apply", msg if show else "", True, branch)

    # ---- 页面：库存 ----
    def page_stock(self, conn, qs):
        branch = qs.get("branch", ["BJ001"])[0]
        ranges = conn.execute(
            "SELECT * FROM stock_ranges WHERE branch_code=? ORDER BY start_no",
            (branch,)).fetchall()
        rtxt = "".join(
            f"<li class=log>{no8(r['start_no'])} – {no8(r['end_no'])}"
            f"（{r['end_no']-r['start_no']+1} 张）</li>" for r in ranges)
        legend = "".join(
            f'<span><i style="background:{c}"></i>{t}</span>'
            for t, c in [("预留", "#c9d6ec"), ("出库", "#1f4e96"),
                         ("启用", "#0b2a5b"), ("已用", "#9aa1ab"),
                         ("止付", "#f3dcd9"), ("作废打孔", "#fff"),
                         ("销毁", "#b3202e")])
        body = f"""
<div class="card"><h2>库存区间登记</h2><ul>{rtxt or '<li class=empty>无</li>'}</ul>
<p class=muted>库存区间之间（如演示数据 10000051–10000059）为缺页带，
预留时任何断号或已占用号都会打断连续性。</p></div>
<div class="card"><h2>全量号码连续条带</h2>
<div class="legend">{legend}<span><i style="background:repeating-linear-gradient(
45deg,#e4dfd2,#e4dfd2 6px,#d7d1c2 6px)"></i>缺页</span></div>
{stock_band(conn, branch)}</div>"""
        return layout(body, "号码段库存", "stock",
                      qs.get("msg", [""])[0], True, branch)

    # ---- 页面：双人出库 ----
    def page_release(self, conn, qs):
        branch = qs.get("branch", ["BJ001"])[0]
        rows = conn.execute(
            "SELECT * FROM checkbooks WHERE branch_code=? AND status=? ORDER BY id",
            (branch, BK_RESERVED)).fetchall()
        trs = []
        for r in rows:
            physical = r["end_no"] - r["start_no"] + 1
            trs.append(f"""<tr><td>{esc(r['apply_ref'])}</td>
<td>{esc(r['acct_no'])} {esc(r['cust_name'])}</td>
<td class=log>{no8(r['start_no'])}–{no8(r['end_no'])}</td>
<td>{physical} 张/本</td>
<td><form method=post action="/books/{r['id']}/release?branch={branch}"
 class="rowline">
{self._teller_opts('teller','T001')}{self._teller_opts('reviewer','T002')}
<button class="btn">共同出库</button></form>
<div class=muted>经办与复核必须为不同账号；重复提交仅首笔成功</div></td></tr>""")
        body = f"""
<div class="card"><h2>待出库支票簿（{len(trs)}）</h2>
<table><thead><tr><th>申请编号</th><th>客户</th><th>号码段</th>
<th>实物数量</th><th>双人出库</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan=5 class=empty>无待出库账册</td></tr>'}</tbody></table></div>"""
        return layout(body, "双人出库", "release",
                      qs.get("msg", [""])[0], True, branch)

    # ---- 页面：签收启用 ----
    def page_signoff(self, conn, qs):
        branch = qs.get("branch", ["BJ001"])[0]
        svc = self.app.service(conn)
        rows = conn.execute(
            "SELECT * FROM checkbooks WHERE branch_code=? AND status IN (?,?)"
            " ORDER BY id", (branch, BK_RELEASED, BK_STOP_PAY)).fetchall()
        cards = []
        for r in rows:
            physical = r["end_no"] - r["start_no"] + 1
            sig = svc.sigs.verify(r["acct_no"])
            refused = r["status"] == BK_STOP_PAY
            result = (f'<span class="pill red">已拒领冻结：{esc(r["refuse_reason"] or "")}</span>'
                      if refused else
                      f'<span class="pill">模拟签样 {sig.ref} 相似度 {sig.confidence:.2f}</span>')
            cards.append(f"""
<div class="card watermark" data-mark="{'拒领止付' if refused else '待签收'}">
<h2>{esc(r['apply_ref'])}　{esc(r['acct_no'])} {esc(r['cust_name'])}
 {stamp(r['status'])}</h2>
<div class="duo">
<div><h4>经办信息（双人出库）</h4>
经办：{esc(r['release_teller'])} 张明<br>复核：{esc(r['release_reviewer'])} 王芳<br>
<span class=muted>出库时间 {esc(r['released_at'])}</span></div>
<div><h4>实物数量核对</h4>
号码段 <span class=log>{no8(r['start_no'])}–{no8(r['end_no'])}</span><br>
应签收 <b>{physical}</b> 张（连续无缺页）</div>
<div><h4>签收结果</h4>{result}
<div class=rowline style="margin-top:8px">
{'<form method=post action="/books/'+str(r['id'])+'/sign?branch='+branch+'" class=rowline>'
 '<button class=btn">客户签收·启用</button>'
 '<label class=fl><input type=checkbox name=force> 签样不符时人工核验放行</label></form>'
 '<form method=post action="/books/'+str(r['id'])+'/refuse?branch='+branch+'" class=rowline>'
 +self._teller_opts('teller','T001')+
 '<input name=reason placeholder="拒领原因" style="width:150px">'
 '<button class="btn red mini">登记拒领</button></form>' if not refused else
 '<span class=muted>号码已全部冻结，等待客户后续领取或销户交回</span>'}
</div></div></div></div>""")
        return layout("".join(cards) or '<div class="card"><div class=empty>'
                      '没有待签收账册</div></div>',
                      "签收启用", "signoff", qs.get("msg", [""])[0], True, branch)

    # ---- 页面：账册详情 ----
    def page_book(self, conn, qs):
        bid = int(qs["bid"][0])
        branch = qs.get("branch", ["BJ001"])[0]
        r = conn.execute("SELECT * FROM checkbooks WHERE id=?", (bid,)).fetchone()
        if r is None:
            return layout('<div class="flash">账册不存在</div>', "404")
        nums = conn.execute(
            "SELECT * FROM check_numbers WHERE book_id=? ORDER BY check_no",
            (bid,)).fetchall()
        voidable = [x for x in nums if x["status"] in
                    {ST_RESERVED, ST_RELEASED, ST_ACTIVE, ST_STOP}]
        band = number_band(nums, r["start_no"], r["end_no"])

        times = []
        for label, t in [("申请", r["created_at"]), ("出库", r["released_at"]),
                         ("签收", r["signed_at"]), ("止付", r["stop_at"]),
                         ("拒领", r["refused_at"])]:
            if t:
                times.append(f"{label} {esc(t)}")
        timeline = " ｜ ".join(times) or esc(r["created_at"])

        # 操作区按状态显隐
        can = {"stop": r["status"] in ("ACTIVE", "RELEASED"),
               "handover": r["status"] in ("ACTIVE", "STOP_PAY", "PART_VOID",
                                           "RELEASED"),
               "used": r["status"] == "ACTIVE"}
        default_nos = "\n".join(no8(x["check_no"]) for x in voidable)
        tel1 = self._teller_opts("teller", "T001")
        tel2 = self._teller_opts("reviewer", "T002")

        if can["stop"]:
            stop_form = (
                f'<form method=post action="/books/{bid}/stop?branch={branch}" '
                f'class=rowline>{tel1}'
                '<input name=reason placeholder="止付原因，如：支票簿遗失" '
                'style="width:240px">'
                '<button class="btn red">挂失止付（冻结未使用号码）</button></form>')
        else:
            stop_form = ('<p class=muted>当前状态不可止付'
                         '（止付迟到：无未使用号码时将拒绝）</p>')

        if can["used"]:
            used_form = (
                f'<form method=post action="/books/{bid}/used?branch={branch}" '
                f'class=rowline><input name=check_no placeholder="8 位号码" '
                f'style="width:140px">{tel1}'
                '<button class="btn ghost mini">登记使用</button></form>')
        else:
            used_form = '<p class=muted>仅启用状态可登记</p>'

        if can["handover"]:
            handover_form = (
                f'<form method=post action="/books/{bid}/handover?branch={branch}">'
                f'<label class=fl>交回实物号码（每行/逗号/空格分隔，共应交回 '
                f'{len(voidable)} 张）</label>'
                f'<textarea name=nos rows=3 style="width:100%">{esc(default_nos)}'
                '</textarea>'
                f'<div class="rowline" style="margin-top:6px">经办 {tel1} '
                f'复核 {tel2}'
                '<button class="btn red">清点交回并打孔作废</button>'
                '<span class=muted>少交即登记“数量不符/缺页”，未交号码维持冻结'
                '</span></div></form>')
        else:
            handover_form = '<p class=muted>当前状态不支持交回</p>'

        actions = f"""
<div class="card"><h2>业务操作</h2>
<h3>挂失止付</h3>{stop_form}
<h3>登记客户已使用号码（模拟签开支票）</h3>{used_form}
<h3>销户：空白页交回清点 + 逐张作废打孔</h3>{handover_form}
<h3>缺页找回：单张补作废</h3>
<form method=post action="/books/{bid}/void-missing?branch={branch}" class=rowline>
<input name=check_no placeholder="8 位缺页号码" style="width:140px">
{tel1}
<button class="btn red mini">补作废打孔</button></form></div>"""

        # 交回单
        ho = conn.execute(
            "SELECT * FROM handovers WHERE book_id=? ORDER BY id", (bid,)).fetchall()
        ho_html = "".join(f"""<tr><td>HO{x['id']:06d}</td>
<td>{x['expected_count']}</td><td>{x['returned_count']}</td>
<td>{'<b style=color:var(--red)>缺 '+str(x['missing_count'])+'</b>'
 if x['missing_count'] else '相符'}</td>
<td>{esc(x['teller_id'])} / {esc(x['reviewer_id'])}</td>
<td>{esc(x['created_at'])}</td>
<td><a class="muted" href="/voucher?path={urllib.parse.quote(esc(x['voucher_path'] or ''))}">凭证</a></td></tr>"""
            for x in ho)

        logs = conn.execute(
            "SELECT * FROM status_log WHERE entity_id IN (?,?) "
            "ORDER BY id DESC LIMIT 30", (str(bid), f"BOOK:{bid}")).fetchall()
        log_rows = "".join(
            f"<tr><td>{esc(x['created_at'])}</td><td>{esc(x['action'])}</td>"
            f"<td>{esc(x['detail'])}</td><td>{esc(x['operator'])}</td></tr>"
            for x in logs)

        body = f"""
<div class="card watermark" data-mark="{'作废' if r['status'] in
 ('PART_VOID','FULLY_VOID') else '销毁' if r['status']=='DESTROYED'
 else '止付' if r['status']=='STOP_PAY' else ''}">
<h2>{esc(r['apply_ref'])}　{stamp(r['status'])}</h2>
<p>账户 {esc(r['acct_no'])} ｜ 户名 {esc(r['cust_name'])} ｜ 网点 {esc(r['branch_code'])}<br>
<span class=muted>{timeline}</span></p>
<h3>号码段连续条带（{no8(r['start_no'])} – {no8(r['end_no'])}）</h3>{band}
<div class="legend">{''.join(f'<span><i class="{cls}" style="display:inline-block;width:14px;height:14px;border:1px solid rgba(0,0,0,.2)"></i>{t}</span>'
 for t, cls in NUM_META.values())}</div></div>
{actions}
<div class="card"><h2>空白页交回 / 打孔记录</h2>
<table><thead><tr><th>交回单</th><th>应交</th><th>实交</th><th>差异</th>
<th>经办/复核</th><th>时间</th><th>凭证</th></tr></thead>
<tbody>{ho_html or '<tr><td colspan=7 class=empty>暂无交回记录</td></tr>'}</tbody></table></div>
<div class="card"><h2>状态审计日志</h2><table class=log>
<thead><tr><th>时间</th><th>动作</th><th>明细</th><th>操作员</th></tr></thead>
<tbody>{log_rows}</tbody></table></div>
<p><a class="btn ghost" href="/?branch={branch}">← 返回台账</a></p>"""
        return layout(body, r["apply_ref"], "", qs.get("msg", [""])[0],
                      True, branch)

    # ---- 页面：销毁台账 ----
    def page_destroy(self, conn, qs):
        branch = qs.get("branch", ["BJ001"])[0]
        batches = conn.execute(
            "SELECT * FROM destroy_batches WHERE branch_code=? ORDER BY id",
            (branch,)).fetchall()
        fully = conn.execute(
            "SELECT * FROM checkbooks WHERE branch_code=? AND status=?",
            (branch, BK_FULLY_VOID)).fetchall()

        create = f"""
<div class="card"><h2>批次操作</h2>
<form method=post action="/batches/create?branch={branch}">
<button class="btn">新建销毁批次</button>
<span class=muted style="margin-left:10px">新建后在下方批次卡片中归集 FULLY_VOID 账册。</span></form>
<p class=muted>作废号码进入销毁批次后即刻置 DESTROYED，不可再次启用。</p></div>"""
        cards = [create]
        for b in batches:
            items = conn.execute(
                "SELECT * FROM destroy_items WHERE batch_id=? ORDER BY book_id,check_no",
                (b["id"],)).fetchall()
            item_rows = "".join(
                f"<tr><td>{esc(x['book_id'])}</td>"
                f"<td class=log>{no8(x['check_no'])}</td>"
                f"<td><span class='stamp stamp-red solid mini'>销毁</span></td></tr>"
                for x in items)
            st = {DEST_OPEN: ("OPEN 待归集/待封存", "stamp-blue"),
                  DEST_CONFIRMED: ("CONFIRMED 双人封存待销毁", "stamp-red"),
                  DEST_DESTROYED: ("DESTROYED 已销毁（不可逆）", "stamp-red solid")}[
                b["status"]]
            forms = ""
            if b["status"] == DEST_OPEN:
                opts = "".join(
                    f'<option value="{r["id"]}">{esc(r["apply_ref"])}</option>'
                    for r in fully)
                forms = f"""
<form method=post action="/batches/{b['id']}/add?branch={branch}" class=rowline>
<label class=fl>归集账册<select name=book_id>{opts or '<option value=0>无</option>'}</select></label>
<button class="btn red mini">归集作废号码入批</button></form>
<form method=post action="/batches/{b['id']}/confirm?branch={branch}" class=rowline>
确认人甲 {self._teller_opts('confirm_a','T001')}
确认人乙 {self._teller_opts('confirm_b','T003')}
<button class="btn">双人确认封存</button><span class=muted>两人必须不同</span></form>"""
            elif b["status"] == DEST_CONFIRMED:
                forms = f"""
<form method=post action="/batches/{b['id']}/destroy?branch={branch}">
<div class=rowline>监销甲 {self._teller_opts('confirm_a','T001')}
监销乙 {self._teller_opts('confirm_b','T003')}
<label class=fl>实销张数<input name=actual_count value="{b['expect_count']}"
 type=number style="width:90px"></label>
<button class="btn red">实物销毁入账</button></div>
<label class=fl style="margin-top:6px">差异说明（实销≠应收 {b['expect_count']} 时必填）
<input name=diff_note style="width:360px"
 placeholder="例：打孔纸屑粘连误计 1 张，已现场复核"></label></form>"""
            else:
                forms = (f'<p>应收 <b>{b["expect_count"]}</b> ｜ 实销 <b>{b["actual_count"]}</b>'
                         f' ｜ 差异 <b style="color:var(--red)">{b["diff_count"]:+d}</b>'
                         f' {esc(b["diff_note"] or "")}<br>'
                         f'<span class=muted>销毁时间 {esc(b["destroyed_at"])} ｜ '
                         f'监销 {esc(b["confirm_a"])} / {esc(b["confirm_b"])} ｜ '
                         f'<a href="/voucher?path={urllib.parse.quote(esc(b["voucher_path"] or ""))}">销毁清单凭证</a></span></p>')
            cards.append(f"""
<div class="card watermark" data-mark="{'' if b['status']==DEST_OPEN else '销毁'}">
<h2>{esc(b['batch_no'])} <span class="stamp {st[1]}">{st[0]}</span></h2>
{forms}
<h3>批次明细（{len(items)} 张）</h3>
<table style="max-height:260px;display:block;overflow:auto"><thead><tr>
<th>账册</th><th>号码</th><th>水印标记</th></tr></thead>
<tbody>{item_rows or '<tr><td colspan=3 class=empty>空批次</td></tr>'}</tbody></table></div>""")
        return layout("".join(cards), "销毁台账", "destroy",
                      qs.get("msg", [""])[0], True, branch)

    # ---- 页面：扫描记录 ----
    def page_scans(self, conn, qs):
        branch = qs.get("branch", ["BJ001"])[0]
        rows = conn.execute(
            "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 40").fetchall()
        trs = "".join(
            f"<tr><td>{esc(r['created_at'])}</td><td>{esc(r['kind'])}</td>"
            f"<td>{esc(r['detail'])}</td></tr>" for r in rows)
        body = f"""
<div class="card"><h2>进程内扫描任务</h2>
<p>守护线程每 30 秒扫描一次 <b>待领/拒领</b> 与 <b>待销毁</b> 记录，
结果写入 scan_runs 表并输出到服务进程控制台。</p>
<form method=post action="/scans/run?branch={branch}">
<button class="btn">立即扫描一轮</button></form></div>
<div class="card"><h2>最近扫描记录</h2><table>
<thead><tr><th>时间</th><th>类型</th><th>结果</th></tr></thead>
<tbody>{trs or '<tr><td colspan=3 class=empty>暂无</td></tr>'}</tbody></table></div>"""
        return layout(body, "扫描记录", "scans", qs.get("msg", [""])[0],
                      True, branch)


worker: ScanWorker = None  # type: ignore


def serve(db_path: str, voucher_root: str, port: int = 8000,
          scan_interval: float = 30.0, do_seed: bool = True) -> None:
    global worker
    if not Path(db_path).exists() and do_seed:
        from .seed import seed
        seed(db_path, voucher_root, reset=True)
    else:
        dbmod.init_db(db_path)
    app = App(db_path, voucher_root)
    Handler.app = app

    worker = ScanWorker(db_path, voucher_root, interval=scan_interval,
                        sink=lambda m: sys.stderr.write(m + "\n"))
    worker.start()

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"支票簿核验系统已启动：http://127.0.0.1:{port}/  （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        worker.stop()
        httpd.server_close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="启动支票簿核验 Web 系统")
    ap.add_argument("--db", default="data/checkbook.db")
    ap.add_argument("--vouchers", default="data/vouchers")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--scan-interval", type=float, default=30.0)
    ap.add_argument("--no-seed", action="store_true")
    args = ap.parse_args(argv)
    serve(args.db, args.vouchers, args.port, args.scan_interval,
          do_seed=not args.no_seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
