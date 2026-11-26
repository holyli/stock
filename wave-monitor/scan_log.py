"""扫描留档与汇总看板。

每次 main.py 扫描结束追加一条 JSONL 到 logs/scan_history.jsonl，并重新渲染
output/扫描看板.html。看板按日期分组、**逐轮记录**：每轮一行，显示该轮的
买卖点与「无信号 / 数据滞后 / 限频跳过」的数量分布。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from utils import sh_now, trading_day_status

PROJECT_DIR = Path(__file__).resolve().parent
HISTORY = PROJECT_DIR / "logs" / "scan_history.jsonl"
BOARD = PROJECT_DIR / "output" / "扫描看板.html"
KEEP_DAYS = 7          # 看板只展示最近 7 天有数据的记录
MAX_LINES = 800        # JSONL 超过这个行数时截断重写，避免无限增长

WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"]


def record_run(items: list[dict], pushed: int, failures: list[str],
               period_filter: str | None = None, started: datetime | None = None) -> None:
    """追加一条运行记录并刷新看板。任何异常都不应影响主流程。"""
    try:
        now = sh_now()
        st = trading_day_status(now)
        rec = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date": st["date"],
            "time": now.strftime("%H:%M"),
            "cn": st["cn"],
            "hk": st["hk"],
            "scope": period_filter or "全部周期",
            "pushed": pushed,
            "failed": len(failures),
            "failures": failures[:10],
            "items": items,
        }
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _trim()
        render_board()
    except Exception:  # noqa: BLE001 - 留档失败不影响推送
        pass


def _trim() -> None:
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_LINES:
            HISTORY.write_text("\n".join(lines[-MAX_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def _load() -> list[dict]:
    if not HISTORY.exists():
        return []
    out = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _weekday(date_str: str) -> str:
    try:
        return "周" + WEEKDAY[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
    except (ValueError, IndexError):
        return ""


def _counts(items: list[dict]) -> dict:
    """单轮的状态计数：命中的不参与统计，四类之和等于标的数。"""
    lim = stale = flat = fail = 0
    for i in items:
        if i.get("signal"):
            continue
        st = str(i.get("status", ""))
        if st == "限频跳过":
            lim += 1
        elif st.startswith("失败"):
            fail += 1
        elif st.startswith("跳过"):
            stale += 1
        elif st == "无信号":
            flat += 1
    return {"lim": lim, "stale": stale, "flat": flat, "fail": fail, "total": len(items)}


def _run_row(r: dict) -> str:
    items = r.get("items") or []
    c = _counts(items)
    hits = [i for i in items if i.get("signal")]

    if hits:
        tags = []
        for h in hits:
            cls = "buy" if h["signal"] == "BUY" else "sell"
            txt = "B 买" if h["signal"] == "BUY" else "S 卖"
            name = h.get("name") or h.get("code", "")
            period = "·日线" if h.get("period") == "daily" else ""
            close = h.get("close")
            price = f" {float(close):.2f}" if isinstance(close, (int, float)) else ""
            tags.append(f'<span class="tag {cls}">{_esc(name)}{period} {txt}{price}</span>')
        sig_html = "".join(tags)
    else:
        sig_html = '<span class="dash">—</span>'

    parts = []
    if c["flat"]:
        parts.append(f'{c["flat"]} 无信号')
    if c["stale"]:
        parts.append(f'<span class="dim">{c["stale"]} 数据滞后</span>')
    if c["fail"]:
        parts.append(f'<b class="fail">{c["fail"]} 取数失败</b>')
    if c["lim"]:
        parts.append(f'<b class="lim">{c["lim"]} 限频跳过</b>')
    summary = (f'{c["total"]} 只 · ' + " · ".join(parts)) if parts else f'{c["total"]} 只'

    clock = str(r.get("ts", ""))[11:19] or r.get("time", "")
    return (f'<tr><td class="t">{_esc(clock)}</td>'
            f'<td class="sig">{sig_html}</td>'
            f'<td class="sm">{summary}</td></tr>')


def render_board() -> Path:
    recs = _load()
    dates = sorted({r.get("date", "") for r in recs})[-KEEP_DAYS:]
    recs = [r for r in recs if r.get("date") in dates]

    by_date: dict[str, list[dict]] = {}
    for r in recs:
        by_date.setdefault(r.get("date", ""), []).append(r)

    total_hits = sum(1 for r in recs for i in (r.get("items") or []) if i.get("signal"))

    blocks = []
    for d in sorted(by_date, reverse=True):
        runs = sorted(by_date[d], key=lambda x: x.get("ts", ""))
        hits_day = sum(1 for r in runs for i in (r.get("items") or []) if i.get("signal"))
        lim_runs = sum(1 for r in runs if _counts(r.get("items") or [])["lim"] > 0)

        meta = []
        if hits_day:
            meta.append(f"命中 {hits_day} 条")
        if lim_runs:
            meta.append(f'<span class="warn">{lim_runs} 轮受限频影响</span>')
        head = (f'<div class="day-head"><b>{_esc(d[5:])}</b>'
                f'<span>{_esc(_weekday(d))}</span>'
                f'<span class="runs">{len(runs)} 轮</span>'
                f'<span class="meta">{" · ".join(meta)}</span></div>')

        table = ('<table><thead><tr><th>时间</th><th>买卖点</th><th>本轮情况</th></tr></thead>'
                 '<tbody>' + "".join(_run_row(r) for r in runs) + '</tbody></table>')
        blocks.append(f'<section class="day">{head}{table}</section>')

    if not blocks:
        blocks = ['<p class="none" style="margin-top:20px">还没有扫描记录。'
                  '跑一次 <code>python main.py</code> 就会出现在这里。</p>']

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="120">
<title>波段扫描看板</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#1a1d21;--dim:#8a9099;--line:#e8eaee;--red:#d92b2b;--grn:#12855a}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:880px;margin:0 auto;padding:30px 20px 60px}}
h1{{font-size:22px;margin:0 0 4px}}
.sub{{color:var(--dim);font-size:12.5px;margin-bottom:20px}}
.day{{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:14px;overflow:hidden}}
.day-head{{display:flex;align-items:baseline;gap:8px;padding:12px 16px;border-bottom:1px solid #f2f3f5;flex-wrap:wrap}}
.day-head b{{font-size:16px;letter-spacing:.5px}}
.day-head span{{font-size:12px;color:var(--dim)}}
.day-head .runs{{background:#f2f4f7;border-radius:20px;padding:1px 8px}}
.day-head .meta{{margin-left:auto;font-size:12px;color:#6b7280}}
.day-head .warn{{color:#c2410c}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th{{text-align:left;padding:7px 16px;font-size:11.5px;color:var(--dim);font-weight:500;background:#fbfbfc}}
th:nth-child(3){{text-align:right}}
td{{padding:9px 16px;border-top:1px solid #f4f5f7;vertical-align:top}}
td.t{{color:#6b7280;font-variant-numeric:tabular-nums;width:62px;white-space:nowrap}}
td.sig{{line-height:1.9}}
td.sm{{text-align:right;color:#6b7280;font-size:12.5px;white-space:nowrap}}
.tag{{display:inline-block;padding:1px 9px;border-radius:20px;font-size:12.5px;font-weight:600;margin-right:6px}}
.tag.buy{{background:#fdecec;color:var(--red)}}
.tag.sell{{background:#e9f6f0;color:var(--grn)}}
.dash{{color:#c7ccd3}}
.lim{{color:#c2410c}}
.fail{{color:#b91c1c}}
.dim{{color:#a3a9b3}}
.none{{margin:0;padding:14px 16px;color:var(--dim);font-size:13.5px}}
code{{background:#f2f4f7;padding:1px 6px;border-radius:4px;font-size:12.5px}}
</style></head><body><div class="wrap">
<h1>波段扫描看板</h1>
<div class="sub">逐轮记录 · 最近 7 天共命中 {total_hits} 条 · 更新于 {sh_now().strftime("%m-%d %H:%M")}</div>
{''.join(blocks)}
</div></body></html>"""

    BOARD.parent.mkdir(parents=True, exist_ok=True)
    BOARD.write_text(html, encoding="utf-8")
    return BOARD


if __name__ == "__main__":
    path = render_board()
    print(f"已生成 {path}")
