"""HTTP 路由：七个台账页面 + 总览 + 事件台账 + 凭证查看。

仅依赖标准库 http.server；POST 执行动作后 303 回跳，业务错误以红条提示。
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import config, services
from .database import connect
from . import views as v


class AppState:
    def __init__(self):
        self.stats_lock = threading.Lock()


def staff_options(conn, role: str | None = None) -> str:
    rows = conn.execute(
        "SELECT account, name FROM staff" + (" WHERE role=?" if role else "") + " ORDER BY account",
        (role,) if role else (),
    ).fetchall()
    return "".join(f'<option value="{v.esc(r["account"])}">{v.esc(r["account"])} {v.esc(r["name"])}</option>' for r in rows)


# ---------------------------------------------------------------- 页面渲染

def page_dashboard(conn, msg="") -> str:
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM check_pages GROUP BY status").fetchall()}
    bk_counts = dict(conn.execute("SELECT status, COUNT(*) FROM booklets GROUP BY status").fetchall())
    pending_apps = conn.execute("SELECT COUNT(*) c FROM applications WHERE status='pending'").fetchone()["c"]
    open_batches = conn.execute(
        "SELECT COUNT(*) c FROM destroy_batches WHERE status IN ('open','discrepancy')").fetchone()["c"]
    stats = [
        ("在库空白", counts.get(config.PAGE_AVAILABLE, 0), ""),
        ("流转中号码", sum(counts.get(s, 0) for s in
         (config.PAGE_RESERVED, config.PAGE_ISSUED, config.PAGE_ACTIVE)), ""),
        ("止付冻结", counts.get(config.PAGE_STOPPED, 0), "red"),
        ("作废待销毁", counts.get(config.PAGE_VOID, 0), "red"),
        ("已销毁", counts.get(config.PAGE_DESTROYED, 0), ""),
        ("待审申请", pending_apps, "red"),
        ("待签收支票簿", bk_counts.get(config.BK_ISSUED, 0), ""),
        ("待销毁批次", open_batches, "red"),
    ]
    stat_html = '<div class="grid grid-4">' + "".join(
        f'<div class="stat {cls}"><div class="n">{n}</div><div class="l">{v.esc(l)}</div></div>'
        for l, n, cls in stats
    ) + "</div>"

    booklets = conn.execute(
        "SELECT b.*, a.account_no, c.name customer_name FROM booklets b "
        "JOIN accounts a ON a.account_no=b.account_no "
        "JOIN customers c ON c.customer_id=a.customer_id ORDER BY b.id").fetchall()
    rows = []
    for b in booklets:
        pages = conn.execute(
            "SELECT check_no, status FROM check_pages WHERE booklet_id=? ORDER BY check_no",
            (b["id"],)).fetchall()
        smap = {p["check_no"]: p["status"] for p in pages}
        latest_void_ts = conn.execute(
            "SELECT MAX(created_at) t FROM voids WHERE booklet_id=?", (b["id"],)).fetchone()["t"]
        strip = v.number_strip(smap, b["start_no"], b["end_no"])
        state = config.BOOKLET_STATUS_LABEL[b["status"]]
        badge = f'<span class="bad-tag">{v.esc(state)}</span>' if b["status"] in (
            config.BK_STOPPED, config.BK_REFUSED, config.BK_CLAIM_FAILED) else v.esc(state)
        wm = v.wm_mark(config.PAGE_VOID, latest_void_ts) if b["status"] == config.BK_REFUSED else ""
        rows.append(
            f"<tr><td>#{b['id']}</td><td>{v.fmt_no(b['start_no'])}–{v.fmt_no(b['end_no'])}</td>"
            f"<td>{v.esc(b['account_no'])}<br><span class='muted'>{v.esc(b['customer_name'])}</span></td>"
            f"<td style='min-width:240px'>{strip}</td><td>{badge}</td><td>{wm}</td>"
            f"<td class='muted'>{v.esc(b['updated_at'])}</td></tr>")
    body = f"""
<h2>总览</h2>{stat_html}
<h2>支票簿号码状态（按连续条带呈现，缺口即不连续）</h2>
<div class="card"><table><thead><tr>
<th>支票簿</th><th>起止号码</th><th>账户 / 客户</th><th>号码条带</th><th>状态</th><th>作废标记与时间</th><th>更新时间</th>
</tr></thead><tbody>{''.join(rows) or '<tr><td colspan=7 class=muted>暂无</td></tr>'}</tbody></table>
{v.strip_legend()}</div>"""
    return v.layout("总览", body, msg)


def page_applications(conn, msg="") -> str:
    apps = conn.execute(
        "SELECT ap.*, a.account_no, c.name customer_name FROM applications ap "
        "JOIN accounts a ON a.account_no=ap.account_no "
        "JOIN customers c ON c.customer_id=a.customer_id ORDER BY ap.id").fetchall()
    reviewer_opts = staff_options(conn, "reviewer")
    rows = []
    for ap in apps:
        action = ""
        if ap["status"] == config.APP_PENDING:
            action = (
                f'<form class="inline" method="post" action="/applications">'
                f'<input type="hidden" name="op" value="review">'
                f'<input type="hidden" name="id" value="{ap["id"]}">'
                f'<select name="reviewer">{reviewer_opts}</select> '
                f'<button name="approve" value="1">审核通过并预留号段</button> '
                f'<button class="danger" name="approve" value="0">驳回</button></form>')
        status_cn = {"pending": "待审核", "approved": "已通过", "rejected": "不通过"}[ap["status"]]
        cls = "ok-tag" if ap["status"] == "approved" else ("bad-tag" if ap["status"] == "rejected" else "")
        bk = conn.execute("SELECT id,start_no,end_no FROM booklets WHERE application_id=?", (ap["id"],)).fetchone()
        seg = f"BK#{bk['id']} {v.fmt_no(bk['start_no'])}–{v.fmt_no(bk['end_no'])}" if bk else '<span class="muted">—</span>'
        rows.append(
            f"<tr><td>#{ap['id']}</td><td>{v.esc(ap['created_at'])}</td>"
            f"<td>{v.esc(ap['account_no'])}<br><span class='muted'>{v.esc(ap['customer_name'])}</span></td>"
            f"<td>{ap['quantity']}</td><td class='{cls}'>{status_cn}</td>"
            f"<td class='muted'>{v.esc(ap['qualifier'] or '')}</td><td>{seg}</td><td>{action}</td></tr>")
    body = f"""
<h2>申请审核</h2>
<div class="card"><p class="muted">审核通过时调用模拟资格适配器核验账户，并立即按连续号码段预留支票簿；
库存被物理缺页断开或可用数不足时，预留失败并保留“待审核”，由号码段库存页追查缺口。</p></div>
<div class="card"><table><thead><tr>
<th>申请</th><th>申请时间</th><th>账户/客户</th><th>数量</th><th>状态</th><th>资格核验结论</th><th>预留号段</th><th>审核操作</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""
    return v.layout("申请审核", body, msg)


def page_inventory(conn, msg="") -> str:
    branch = conn.execute("SELECT code, name FROM branches ORDER BY code LIMIT 1").fetchone()
    rows_pg = conn.execute(
        "SELECT check_no, status FROM check_pages WHERE branch_code=? ORDER BY check_no",
        (branch["code"],)).fetchall()
    smap = {r["check_no"]: r["status"] for r in rows_pg}
    lo, hi = min(smap), max(smap)
    gaps = {r["check_no"] for r in conn.execute(
        "SELECT check_no FROM stock_gaps WHERE branch_code=?", (branch["code"],)).fetchall()}
    strip = v.number_strip(smap, lo, hi, gaps)

    free = [n for n, s in smap.items() if s == config.PAGE_AVAILABLE]
    bands = services.segment_bands(free)
    band_rows = "".join(
        f"<tr><td>{v.fmt_no(a)}</td><td>{v.fmt_no(b)}</td><td>{b - a + 1}</td></tr>"
        for a, b in bands)
    gap_rows = "".join(
        f"<tr><td>{v.fmt_no(g['check_no'])}</td><td>{v.esc(g['reason'])}</td><td>{v.esc(g['created_at'])}</td></tr>"
        for g in conn.execute("SELECT * FROM stock_gaps ORDER BY check_no"))

    # 新申请入口：直接在库存页提交
    acct_opts = "".join(
        f'<option value="{v.esc(r["account_no"])}">{v.esc(r["account_no"])}（{v.esc(r["name"])}）</option>'
        for r in conn.execute(
            "SELECT a.account_no, c.name FROM accounts a JOIN customers c ON c.customer_id=a.customer_id "
            "WHERE a.status='normal' ORDER BY a.account_no"))
    body = f"""
<h2>号码段库存 · {v.esc(branch['name'])}（{v.esc(branch['code'])}）</h2>
<div class="card"><div style="margin-bottom:8px">{strip}</div>{v.strip_legend()}
<p class="muted" style="margin-top:8px">全库存 {lo:08d}–{hi:08d}；红斜纹为物理缺页，状态色块在缺页处断开，
系统只在不间断的连续条带内预留支票簿。</p></div>
<div class="grid grid-3">
<div class="card"><h3 style="color:var(--blue);margin-top:0">可连续预留条带（按起止号）</h3>
<table><thead><tr><th>起号</th><th>止号</th><th>张数</th></tr></thead><tbody>{band_rows}</tbody></table></div>
<div class="card"><h3 style="color:var(--red);margin-top:0">号码缺页登记</h3>
<table><thead><tr><th>号码</th><th>原因</th><th>登记时间</th></tr></thead><tbody>{gap_rows}</tbody></table></div>
<div class="card"><h3 style="color:var(--blue);margin-top:0">提交领用申请</h3>
<form method="post" action="/applications">
<input type="hidden" name="op" value="submit">
<p>账户：<select name="account_no">{acct_opts}</select></p>
<p>数量：<input name="quantity" type="number" min="1" value="10" style="width:90px"> 张</p>
<p><button>提交申请（转审核）</button></p>
<p class="muted">提示：数量 55 将因缺页无法取得连续号段。</p></form></div></div>"""
    return v.layout("号码段库存", body, msg)


def page_disburse(conn, msg="") -> str:
    pending = conn.execute(
        "SELECT b.*, a.account_no, c.name customer_name FROM booklets b "
        "JOIN accounts a ON a.account_no=b.account_no "
        "JOIN customers c ON c.customer_id=a.customer_id WHERE b.status='reserved' ORDER BY b.id").fetchall()
    teller_opts = staff_options(conn, "teller")
    reviewer_opts = staff_options(conn, "reviewer")
    forms = "".join(
        f"<tr><td>#{b['id']}</td><td>{v.fmt_no(b['start_no'])}–{v.fmt_no(b['end_no'])}（{b['end_no']-b['start_no']+1} 张）</td>"
        f"<td>{v.esc(b['account_no'])} <span class='muted'>{v.esc(b['customer_name'])}</span></td>"
        f"<td><form class='inline' method='post' action='/disburse'>"
        f"<input type='hidden' name='op' value='disburse'><input type='hidden' name='id' value='{b['id']}'>"
        f"经办 <select name='handler'>{teller_opts}</select> "
        f"复核 <select name='reviewer'>{reviewer_opts}</select> "
        f"实物 <input name='physical_qty' type='number' value='{b['end_no']-b['start_no']+1}' style='width:64px'> 张 "
        f"<button>双人出库</button></form></td></tr>" for b in pending)

    # 双人复核台账：经办信息 | 实物数量 | 签收结果 三列对齐
    records = conn.execute(
        "SELECT d.*, b.start_no, b.end_no, b.status bk_status, "
        "(SELECT result FROM signoffs s WHERE s.booklet_id=b.id ORDER BY s.id DESC LIMIT 1) last_sign, "
        "(SELECT note FROM signoffs s WHERE s.booklet_id=b.id ORDER BY s.id DESC LIMIT 1) last_note "
        "FROM disbursals d JOIN booklets b ON b.id=d.booklet_id ORDER BY d.id").fetchall()
    rec_rows = []
    for d in records:
        h = conn.execute("SELECT name FROM staff WHERE account=?", (d["handler"],)).fetchone()["name"]
        r = conn.execute("SELECT name FROM staff WHERE account=?", (d["reviewer"],)).fetchone()["name"]
        sign_cn = {"signed": "<span class='ok-tag'>签收成功，已启用</span>",
                   "failed": "<span class='bad-tag'>签收失败，待重签</span>",
                   "refused": "<span class='bad-tag'>客户拒领，已作废</span>"}.get(d["last_sign"], '<span class="muted">待客户签收</span>')
        sep = (f'<span class="ok-tag">账号不同 ✓</span>' if d["handler"] != d["reviewer"]
               else '<span class="bad-tag">同人违规 ✗</span>')
        rec_rows.append(
            f"<tr><td>#{d['booklet_id']}<br><span class='muted'>{v.fmt_no(d['start_no'])}–{v.fmt_no(d['end_no'])}</span></td>"
            f"<td>经办 {v.esc(d['handler'])} {v.esc(h)}<br>复核 {v.esc(d['reviewer'])} {v.esc(r)}<br>{sep}<br>"
            f"<span class='muted'>{v.esc(d['created_at'])}</span></td>"
            f"<td style='text-align:center;font-size:16px'><b>{d['physical_qty']}</b> 张<br>"
            f"<span class='muted'>应 {d['end_no']-d['start_no']+1} 张，"
            f"{'一致' if d['physical_qty']==d['end_no']-d['start_no']+1 else '不符'}</span></td>"
            f"<td>{sign_cn}<div class='muted'>{v.esc(d['last_note'] or '')}</div></td></tr>")
    body = f"""
<h2>双人出库</h2>
<div class="card"><p class="muted">经办柜员与复核柜员必须为<b>不同员工账号</b>，并核对实物数量与号段数量一致方可出库；
两个柜员同时抢占同一支票簿时，仅首笔成功（数据库行级状态条件保证）。</p></div>
<div class="card"><table><thead><tr><th>预留支票簿</th><th>号段</th><th>账户/客户</th><th>双人出库操作</th></tr></thead>
<tbody>{forms or '<tr><td colspan=4 class=muted>暂无待出库</td></tr>'}</tbody></table></div>
<h2>双人复核台账（经办信息 · 实物数量 · 签收结果三列对齐）</h2>
<div class="card"><table class="three-col"><thead><tr><th>支票簿</th><th>经办信息（经办 / 复核分离）</th>
<th>实物数量核对</th><th>客户签收结果</th></tr></thead><tbody>{''.join(rec_rows) or '<tr><td colspan=4 class=muted>暂无</td></tr>'}</tbody></table></div>"""
    return v.layout("双人出库", body, msg)


def page_signoff(conn, msg="") -> str:
    waiting = conn.execute(
        "SELECT b.*, c.name customer_name FROM booklets b "
        "JOIN accounts a ON a.account_no=b.account_no JOIN customers c ON c.customer_id=a.customer_id "
        "WHERE b.status IN ('issued','claim_failed') ORDER BY b.id").fetchall()
    rows = []
    for b in waiting:
        tip = "客户到场，请求签名适配器" if b["status"] == config.BK_ISSUED else "签名不符，客户重新到场签收"
        rows.append(
            f"<tr><td>#{b['id']}</td><td>{v.fmt_no(b['start_no'])}–{v.fmt_no(b['end_no'])}</td>"
            f"<td>{v.esc(b['customer_name'])}</td><td>{v.esc(tip)}</td>"
            f"<td><form class='inline' method='post' action='/signoff'><input type='hidden' name='op' value='sign'>"
            f"<input type='hidden' name='id' value='{b['id']}'><button>发起客户签收</button></form></td></tr>")
    records = conn.execute(
        "SELECT s.*, b.start_no, b.end_no FROM signoffs s JOIN booklets b ON b.id=s.booklet_id ORDER BY s.id DESC").fetchall()
    rec = []
    for s in records:
        result_cn = {"signed": "<span class='ok-tag'>签收成功并启用</span>",
                     "failed": "<span class='bad-tag'>签收失败（保留待重签，不启用）</span>",
                     "refused": "<span class='bad-tag'>客户拒领（已逐张作废）</span>"}[s["result"]]
        link = v.page_link(s["voucher_path"], "签收凭证") if s["voucher_path"] else '<span class="muted">—</span>'
        rec.append(
            f"<tr><td>#{s['booklet_id']} <span class='muted'>{v.fmt_no(s['start_no'])}–{v.fmt_no(s['end_no'])}</span></td>"
            f"<td>{result_cn}</td><td>{v.esc(s['note'])}</td><td>{v.esc(s['signature'] or '')}</td>"
            f"<td>{link}</td><td class='muted'>{v.esc(s['created_at'])}</td></tr>")
    body = f"""
<h2>签收启用</h2>
<div class="card"><p class="muted">签收成功后支票簿方可启用；签名不符记“签收失败”，号码保持已出库待签收，可重新发起；
客户拒领则全部空白页当场逐张打孔作废。重复签收会被系统拒绝。</p></div>
<div class="card"><table><thead><tr><th>支票簿</th><th>号段</th><th>客户</th><th>说明</th><th>操作</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=5 class=muted>暂无待签收</td></tr>'}</tbody></table></div>
<h2>签收记录</h2>
<div class="card"><table><thead><tr><th>支票簿</th><th>结果</th><th>适配器说明</th><th>签名</th><th>凭证</th><th>时间</th></tr></thead>
<tbody>{''.join(rec)}</tbody></table></div>"""
    return v.layout("签收启用", body, msg)


def page_stop(conn, msg="") -> str:
    targets = conn.execute(
        "SELECT b.*, c.name FROM booklets b JOIN accounts a ON a.account_no=b.account_no "
        "JOIN customers c ON c.customer_id=a.customer_id "
        "WHERE b.status IN ('active','issued','claim_failed') ORDER BY b.id").fetchall()
    opts = "".join(
        f'<option value="{b["id"]}">#{b["id"]} {v.fmt_no(b["start_no"])}–{v.fmt_no(b["end_no"])} {v.esc(b["name"])}</option>'
        for b in targets)
    form = f"""
<div class="card"><form method="post" action="/stop">
<input type="hidden" name="op" value="stop">
<p>支票簿：<select name="id">{opts or '<option>（无）</option>'}</select></p>
<p>已签发张数（号段前段不冻结）：<input name="used_qty" type="number" min="0" value="0" style="width:80px"></p>
<p>止付原因：<input name="reason" type="text" value="客户挂失，支票簿遗失（虚构）" style="width:380px"></p>
<p><button class="danger">发起挂失止付</button></p></form>
<p class="muted">止付仅冻结未使用号码；若通知到达时号码已全部作废或销毁，则登记为止付迟到，不产生冻结。</p></div>"""
    orders = conn.execute("SELECT * FROM stop_orders ORDER BY id DESC").fetchall()
    rows = []
    for o in orders:
        tag = '<span class="bad-tag">止付迟到，无可冻结号码</span>' if o["late"] else f'冻结 <b>{o["frozen_qty"]}</b> 个未使用号码'
        rows.append(
            f"<tr><td>#{o['id']}</td><td>#{o['booklet_id']}</td><td>{v.esc(o['reason'])}</td>"
            f"<td>{tag}</td><td>{v.esc(o['status'])}</td><td class='muted'>{v.esc(o['created_at'])}</td></tr>")
    body = f"""
<h2>挂失止付</h2>{form}
<h2>止付台账</h2>
<div class="card"><table><thead><tr><th>止付单</th><th>支票簿</th><th>原因</th><th>冻结结果</th><th>状态</th><th>时间</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=6 class=muted>暂无</td></tr>'}</tbody></table></div>"""
    return v.layout("挂失止付", body, msg)


def page_returns(conn, msg="") -> str:
    targets = conn.execute(
        "SELECT b.*, c.name FROM booklets b JOIN accounts a ON a.account_no=b.account_no "
        "JOIN customers c ON c.customer_id=a.customer_id "
        "WHERE b.status IN ('active','stopped') ORDER BY b.id").fetchall()
    teller_opts = staff_options(conn, "teller")
    cards = []
    for b in targets:
        exp = conn.execute(
            "SELECT COUNT(*) c FROM check_pages WHERE booklet_id=? AND status IN ('active','stopped')",
            (b["id"],)).fetchone()["c"]
        cards.append(f"""
<div class="card"><b>#{b['id']} {v.fmt_no(b['start_no'])}–{v.fmt_no(b['end_no'])}</b>
<span class="muted">{v.esc(b['name'])} · 应空白页 {exp} 张</span>
<form method="post" action="/returns" style="margin-top:8px">
<input type="hidden" name="op" value="return"><input type="hidden" name="id" value="{b['id']}">
实交 <input name="actual_qty" type="number" min="0" value="{exp}" style="width:70px"> 张
经办 <select name="handler">{teller_opts}</select>
<button class="danger">销户清点并逐张作废</button></form></div>""")
    records = conn.execute("SELECT * FROM returns ORDER BY id DESC").fetchall()
    rows = []
    for r in records:
        if r["mismatch"]:
            res = '<span class="bad-tag">数量不符（挂账追查，未交号码保持原状态）</span>'
        else:
            res = '<span class="ok-tag">数量一致，已逐张作废</span>'
        rows.append(
            f"<tr><td>#{r['id']}</td><td>#{r['booklet_id']}</td><td>应收 {r['expected_qty']} / 实收 {r['actual_qty']}</td>"
            f"<td>{res}</td><td>{v.esc(r['handler'])}</td><td class='muted'>{v.esc(r['created_at'])}</td></tr>")
    body = f"""
<h2>空白页交回（销户清点）</h2>
<div class="card"><p class="muted">销户时逐张清点空白页并打孔作废；实交与应收不符时，差异号码不做作废、保持原状态并挂账追查。</p></div>
{''.join(cards) or '<div class="card muted">暂无需交回的支票簿</div>'}
<h2>交回台账</h2>
<div class="card"><table><thead><tr><th>记录</th><th>支票簿</th><th>数量核对</th><th>处理结果</th><th>经办</th><th>时间</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=6 class=muted>暂无</td></tr>'}</tbody></table></div>"""
    return v.layout("空白页交回", body, msg)


def page_destroy(conn, msg="") -> str:
    void_rows = conn.execute(
        "SELECT p.check_no, p.updated_at, v.reason, v.operator FROM check_pages p "
        "LEFT JOIN voids v ON v.branch_code=p.branch_code AND v.check_no=p.check_no "
        "WHERE p.status='void' ORDER BY p.check_no").fetchall()
    bands = services.segment_bands([r["check_no"] for r in void_rows])
    band_html = " ".join(
        f'<span class="seg" style="position:relative;display:inline-block;flex:none;padding:2px 10px;background:#8a8a8a;color:#fff;border-radius:3px;margin:2px">'
        f'{v.fmt_no(a)}–{v.fmt_no(b)} <span class="muted" style="color:#eee">×{b-a+1}</span></span>'
        for a, b in bands) or '<span class="muted">无待销毁号码</span>'
    sup_opts = staff_options(conn, "supervisor")
    form = f"""
<div class="card"><h3 style="margin-top:0;color:var(--blue)">作废待销毁号码（连续条带）</h3>
<div style="line-height:2.2">{band_html}</div>
<form method="post" action="/destroy" style="margin-top:10px">
<input type="hidden" name="op" value="batch"><button>扫描汇集为销毁批次</button></form></div>"""

    batches = conn.execute("SELECT * FROM destroy_batches ORDER BY id DESC").fetchall()
    blocks = []
    for b in batches:
        items = conn.execute("SELECT check_no, destroyed FROM destroy_items WHERE batch_id=? ORDER BY check_no", (b["id"],)).fetchall()
        ibands = services.segment_bands([r["check_no"] for r in items])
        strip = " ".join(f'<b style="color:var(--blue)">{v.fmt_no(a)}–{v.fmt_no(b)}</b>（{b-a+1}张）' for a, b in ibands)
        if b["status"] == config.BATCH_CONFIRMED:
            marks = "".join(
                f'<div style="margin:3px 0">{v.wm_mark(config.PAGE_DESTROYED, b["confirmed_at"])} '
                f'<span class="muted">双人 {v.esc(b["confirmer_a"])} / {v.esc(b["confirmer_b"])}，终态不可逆</span></div>')[:400]
            head = '<span class="ok-tag">已确认销毁 · 终态，不可逆</span>'
            action = ""
        elif b["status"] == config.BATCH_DISCREPANCY:
            head = '<span class="bad-tag">销毁差异挂账，待复核后重新确认</span>'
            action = _destroy_confirm_form(b, sup_opts)
            marks = ""
        else:
            head = '<span class="bad-tag">待双人确认</span>'
            action = _destroy_confirm_form(b, sup_opts)
            marks = ""
        vlink_row = conn.execute("SELECT path FROM vouchers WHERE kind='destroy' AND ref_no=? LIMIT 1",
                                 (f"DB{b['id']:04d}",)).fetchone()
        vlink = v.page_link(vlink_row["path"], "销毁凭证") if vlink_row else ""
        blocks.append(f"""
<div class="card"><h3 style="margin:4px 0">批次 DB{b['id']:04d} · {v.esc(b['branch_code'])} {head}</h3>
<p class="muted">号码：{strip}<br>应销 {b['expected_qty']} 张 / 现场 {v.esc(b['actual_qty'] if b['actual_qty'] is not None else '—')} 张 {vlink}</p>
{action}{marks}</div>""")
    body = f"""
<h2>销毁台账</h2>{form}
<h2>销毁批次</h2>{''.join(blocks) or '<div class="card muted">暂无批次</div>'}"""
    return v.layout("销毁台账", body, msg)


def _destroy_confirm_form(b, sup_opts) -> str:
    return f"""<form method="post" action="/destroy">
<input type="hidden" name="op" value="confirm"><input type="hidden" name="id" value="{b['id']}">
现场清点 <input name="actual_qty" type="number" min="0" value="{b['expected_qty']}" style="width:80px"> 张，
双人确认：<select name="confirmer_a">{sup_opts}</select>
<select name="confirmer_b">{sup_opts}</select>
<button class="danger">双人监销确认</button>
<p class="muted">两名确认人账号必须不同；清点数量与应销数量不符时批次挂账，不执行销毁。已确认批次不可再次确认，号码永不可再启用。</p></form>"""


def page_ledger(conn, msg="") -> str:
    rows = conn.execute("SELECT * FROM event_log ORDER BY id DESC LIMIT 200").fetchall()
    trs = "".join(
        f"<tr><td class='cat'>{v.esc(r['category'])}</td><td>{v.esc(r['message'])}</td><td class='muted'>{v.esc(r['created_at'])}</td></tr>"
        for r in rows)
    body = f"""
<h2>事件台账</h2>
<div class="card"><p class="muted">申请、预留、双人出库、签收、止付、交回、作废、销毁与进程内扫描的全部处理记录（最近 200 条）。</p></div>
<div class="card"><table class="evlog"><thead><tr><th>类别</th><th>处理记录</th><th>时间</th></tr></thead>
<tbody>{trs}</tbody></table></div>"""
    return v.layout("事件台账", body, msg)


def page_voucher(conn, rel_path: str) -> tuple[str, str]:
    # 严格限制在凭证目录内，防路径穿越（数据库中仅存文件名）
    base = (config.VOUCHER_DIR / Path(rel_path).name).resolve()
    vdir = config.VOUCHER_DIR.resolve()
    if vdir not in base.parents or not base.exists():
        body = '<h2>凭证不存在</h2><div class="card">凭证不存在或已被清理。</div>'
        return v.layout("凭证不存在", body), "text/html; charset=utf-8"
    text = base.read_text(encoding="utf-8")
    body = f'<h2>{v.esc(vouchers_pretty_kind(base.name))}</h2><div class="card"><pre class="voucher">{v.esc(text)}</pre></div>'
    return v.layout("凭证", body), "text/html"


def vouchers_pretty_kind(name: str) -> str:
    if name.startswith("signoff"):
        return "客户签收凭证（虚构）"
    if name.startswith("void"):
        return "支票作废（打孔）凭证（虚构）"
    if name.startswith("destroy"):
        return "支票销毁凭证（虚构）"
    return "凭证"


# ---------------------------------------------------------------- POST 动作

def handle_post(path: str, form: dict) -> str:
    op = form.get("op", [""])[0]
    conn = connect()
    try:
        if path == "/applications" and op == "submit":
            app_id = services.submit_application(
                conn, "BJ001", form["account_no"][0], int(form["quantity"][0]))
            return f"/applications?msg={quote(f'申请 #{app_id} 已提交，等待审核')}"
        if path == "/applications" and op == "review":
            res = services.review_application(
                conn, int(form["id"][0]), form["reviewer"][0], form.get("approve") == ["1"])
            if res["status"] == config.APP_APPROVED:
                m = f"审核通过，预留连续号段 {res['start_no']:08d}-{res['end_no']:08d}"
            else:
                m = "已驳回/资格不通过：" + (res.get("qualifier") or "")
            return f"/applications?msg={quote(m)}"
        if path == "/disburse" and op == "disburse":
            did = services.disburse(conn, int(form["id"][0]), form["handler"][0],
                                    form["reviewer"][0], int(form["physical_qty"][0]))
            return f"/disburse?msg={quote(f'出库记录 #{did} 完成（经办与复核分离，实物数量一致）')}"
        if path == "/signoff" and op == "sign":
            res = services.customer_signoff(conn, int(form["id"][0]))
            m = {"signed": "签收成功，支票簿已启用",
                 "failed": "签收失败：" + res["note"] + "，可重新发起",
                 "refused": f"客户拒领，{res['voided']} 张已逐张作废"}[res["result"]]
            return f"/signoff?msg={quote(m)}"
        if path == "/stop" and op == "stop":
            res = services.stop_payment(conn, int(form["id"][0]), form["reason"][0],
                                        int(form["used_qty"][0]))
            if res["late"]:
                m = "止付迟到：已无可冻结的未使用号码"
            else:
                m = f"挂失止付完成，冻结 {res['frozen_qty']} 个未使用号码"
            return f"/stop?msg={quote(m)}"
        if path == "/returns" and op == "return":
            res = services.return_blank_pages(conn, int(form["id"][0]),
                                              int(form["actual_qty"][0]), form["handler"][0])
            if res["mismatch"]:
                m = f"交回数量不符：应收 {res['expected_qty']} 实收 {res['actual_qty']}，差异挂账追查"
            else:
                m = f"销户交回 {res['voided']} 张，数量一致并逐张作废"
            return f"/returns?msg={quote(m)}"
        if path == "/destroy" and op == "batch":
            bid = services.create_destroy_batch(conn, "BJ001")
            return f"/destroy?msg={quote(f'销毁批次 DB{bid:04d} 已建立，等待双人确认')}"
        if path == "/destroy" and op == "confirm":
            res = services.confirm_destroy(conn, int(form["id"][0]), form["confirmer_a"][0],
                                           form["confirmer_b"][0], int(form["actual_qty"][0]))
            if res["status"] == config.BATCH_CONFIRMED:
                m = f"双人确认完成，销毁 {res['destroyed']} 张，终态不可逆"
            else:
                m = f"销毁差异：应销 {res['expected']} 张，实收 {res['actual']} 张，批次挂账"
            return f"/destroy?msg={quote(m)}"
        return path + "?msg=" + quote("未知操作")
    except services.ServiceError as e:
        return f"{path}?msg={quote('业务校验失败：' + str(e))}"
    finally:
        conn.close()


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    def _send(self, content: str, status: int = 200, ctype: str = "text/html; charset=utf-8", redirect: str | None = None):
        if redirect:
            self.send_response(303)
            self.send_header("Location", redirect)
            self.end_headers()
            return
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        msg = qs.get("msg", [""])[0]
        conn = connect()
        try:
            if parsed.path == "/voucher":
                content, ctype = page_voucher(conn, qs.get("path", [""])[0])
                self._send(content, ctype=ctype)
                return
            pages = {
                "/": page_dashboard, "/applications": page_applications, "/inventory": page_inventory,
                "/disburse": page_disburse, "/signoff": page_signoff, "/stop": page_stop,
                "/returns": page_returns, "/destroy": page_destroy, "/ledger": page_ledger,
            }
            fn = pages.get(parsed.path)
            if fn is None:
                self._send(v.layout("未找到", "<h2>404</h2>"), 404)
                return
            self._send(fn(conn, msg))
        finally:
            conn.close()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        try:
            redirect = handle_post(parsed.path, form)
        except services.ServiceError as e:
            redirect = f"{parsed.path}?msg={quote('业务校验失败：' + str(e))}"
        self._send("", redirect=redirect)

    def log_message(self, fmt, *args):
        print(f"[web] {self.address_string()} {fmt % args}")


def make_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)
