#!/usr/bin/env python3
"""把「表格真值」与「我们自己算出的信号」叠画在同一张 K 线图上。

标注约定（用户 2026-11-25 指定）：
  ○ 空心圆圈 = 表格里的买卖点（真值）
  ▲▼ 实心三角 = 我们的算法算出的买卖点
  - 买点画在 K 线低点下方，卖点画在高点上方
  - 真值在近处一层、算法在远处一层，避免同根重叠

命中判定：同向（买对买、卖对卖）且相差 <= 1 根 K 线。

用法: .venv/bin/python tools/plot_truth_vs_algo.py
输出: output/真值vs算法_K线标注.html
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from main import calculate  # noqa: E402

CACHE = ROOT / "data" / "calib"
OUT = ROOT / "output" / "真值vs算法_K线标注.html"

FS = {"sz300251": 27.53e8, "sz300750": 42.61e8, "sz302132": 5.86e8}
NAME = {"sz300251": "光线传媒", "sz300750": "宁德时代", "sz302132": "中航成飞"}

PAD_DAY, PAD_M60 = 30, 40          # 真值窗口左右各留多少根
W, PANEL_H = 1660, 660
ML, MR, MT, MB = 56, 76, 78, 62    # 绘图区内边距
TRUTH_GAP, ALGO_GAP = 17, 50       # 两套标注的垂直轨道（px）


# ────────────────────────── 数据与信号 ──────────────────────────
def idx_of(df, ts, period):
    if period == "day":
        hit = df.index[df["time"].astype(str).str[:10] == ts]
        return int(hit[0]) if len(hit) else None
    d, t = ts.split(" ")
    hh, mm = map(int, t.split(":"))
    m = hh * 60 + mm
    best = min([630, 690, 840, 900], key=lambda c: abs(c - m))
    lab = f"{best // 60:02d}:{best % 60:02d}:00"
    hit = df.index[(df["time"].astype(str).str[:10] == d) & (df["time"].astype(str).str[11:] == lab)]
    return int(hit[0]) if len(hit) else None


def load_one(code, period, truth, cfg):
    df = pd.read_csv(CACHE / f"{code}_{period}.csv")
    df["time"] = df["time"].astype(str)
    if period == "m60":
        df["turnover"] = df["volume"] * 100.0 / FS[code] * 100.0
    cfg_period = "daily" if period == "day" else period
    res = calculate(df.copy(), cfg, cfg_period)

    ti, ts = [], []
    for t, s in truth:
        i = idx_of(res, t, period)
        if i is not None:
            ti.append(i)
            ts.append(s)
    ti = np.array(ti)
    ts = np.array(ts)

    buy = np.where(res["buy"].to_numpy())[0]
    sell = np.where(res["sell"].to_numpy())[0]

    lo, hi = int(ti.min()), int(ti.max())
    pad = PAD_DAY if period == "day" else PAD_M60
    w0 = max(0, lo - pad)
    w1 = min(len(res) - 1, hi + pad)

    # 窗口内信号
    sb = [i for i in buy if w0 <= i <= w1]
    ss = [i for i in sell if w0 <= i <= w1]
    # 统计用真值窗口（不做 pad）
    sb_eval = [i for i in buy if lo - 1 <= i <= hi + 1]
    ss_eval = [i for i in sell if lo - 1 <= i <= hi + 1]

    hit_s = 0
    for i in sb_eval:
        if len(ti[ts == 1]) and np.min(np.abs(ti[ts == 1] - i)) <= 1:
            hit_s += 1
    for i in ss_eval:
        if len(ti[ts == -1]) and np.min(np.abs(ti[ts == -1] - i)) <= 1:
            hit_s += 1
    n_sig = len(sb_eval) + len(ss_eval)

    hit_t = 0
    for i, s in zip(ti, ts):
        cand = sb_eval if s == 1 else ss_eval
        if cand and np.min(np.abs(np.array(cand) - i)) <= 1:
            hit_t += 1

    return dict(code=code, period=period, name=NAME[code], df=res, w0=w0, w1=w1,
                ti=ti, ts=ts, buy=sb, sell=ss,
                n_sig=n_sig, hit_sig=hit_s, n_truth=len(ti), hit_truth=hit_t)


# ────────────────────────── 绘制 ──────────────────────────
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render(d):
    df = d["df"]
    w0, w1 = d["w0"], d["w1"]
    eval_lo, eval_hi = int(d["ti"].min()), int(d["ti"].max())
    sub = df.iloc[w0:w1 + 1].reset_index(drop=True)
    n = len(sub)
    plot_w = W - ML - MR
    step = plot_w / n
    body_w = max(1.6, step * 0.62)

    pmax = float(sub["high"].max())
    pmin = float(sub["low"].min())
    if pmax <= pmin:
        pmax = pmin + max(0.01, pmin * 0.02)
    # 上下各预留固定像素给标注轨道（真值 17px + 算法 50px + 半径），彻底避免裁切
    y_top = MT + 62
    y_bot = PANEL_H - MB - 62

    def X(i):
        return ML + step * (i + 0.5)

    def Y(p):
        return y_top + (pmax - p) / (pmax - pmin) * (y_bot - y_top)

    # 均线
    ma7 = sub["close"].rolling(7).mean().to_numpy()
    prev_close = df["close"].shift(1).iloc[w0:w1 + 1].to_numpy()

    parts = []
    # 网格与价格刻度
    for k in range(6):
        p = pmax - (pmax - pmin) * k / 5
        y = Y(p)
        parts.append(f'<line x1="{ML}" y1="{y:.1f}" x2="{W-MR}" y2="{y:.1f}" stroke="#e8eaee" stroke-width="1"/>')
        parts.append(f'<text x="{W-MR+8}" y="{y+4:.1f}" font-size="11" fill="#8a9099">{p:.2f}</text>')

    # K 线
    for i, r in sub.iterrows():
        x = X(i)
        up = r["close"] >= r["open"]
        col = "#d92b2b" if up else "#12855a"
        parts.append(f'<line x1="{x:.1f}" y1="{Y(r["high"]):.1f}" x2="{x:.1f}" y2="{Y(r["low"]):.1f}" stroke="{col}" stroke-width="1"/>')
        y1, y2 = Y(max(r["open"], r["close"])), Y(min(r["open"], r["close"]))
        h = max(1.2, y2 - y1)
        fill = col if up else col
        parts.append(f'<rect x="{x-body_w/2:.1f}" y="{y1:.1f}" width="{body_w:.1f}" height="{h:.1f}" fill="{fill}" '
                     f'stroke="{col}" stroke-width="0.6"/>')

    # MA7
    pts = []
    for i in range(n):
        if not np.isnan(ma7[i]):
            pts.append(f"{X(i):.1f},{Y(ma7[i]):.1f}")
    if pts:
        parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="#2f7ed8" stroke-width="1.8" opacity="0.9"/>')

    # 日期轴
    last_lab = None
    lab_step = 8 if d["period"] == "m60" else 6
    for i in range(0, n, lab_step):
        t = str(sub["time"].iloc[i])
        day = t[5:10].replace("-", "/")
        if d["period"] == "m60":
            hm = t[11:16]
            lab = f"{day}<tspan x=\"{X(i):.1f}\" dy=\"13\">{hm}</tspan>"
        else:
            lab = day
        if lab == last_lab:
            continue
        last_lab = lab
        parts.append(f'<text x="{X(i):.1f}" y="{PANEL_H-MB+22}" font-size="10.5" fill="#8a9099" text-anchor="middle">{lab}</text>')

    # ── 真值：空心圆 ──
    for i, s in zip(d["ti"], d["ts"]):
        if not (w0 <= i <= w1):
            continue
        j = i - w0
        r = sub.iloc[j]
        if s == 1:
            x, y = X(j), Y(r["low"]) - TRUTH_GAP
            col = "#d92b2b"
        else:
            x, y = X(j), Y(r["high"]) + TRUTH_GAP
            col = "#12855a"
        y = min(max(y, MT + 9), PANEL_H - MB - 9)
        tip = f'表格真值 {"买" if s==1 else "卖"} · {str(r["time"])[:16]}'
        parts.append(f'<g><title>{esc(tip)}</title>'
                     f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="#fff" fill-opacity="0.95" '
                     f'stroke="{col}" stroke-width="2.4"/></g>')

    # ── 算法：实心三角 ──
    for i in d["buy"]:
        if not (w0 <= i <= w1):
            continue
        j = i - w0
        r = sub.iloc[j]
        x, y = X(j), Y(r["low"]) - ALGO_GAP
        y = min(max(y, MT + 10), PANEL_H - MB - 10)
        if eval_lo - 1 <= i <= eval_hi + 1:
            poly = f'{x:.1f},{y-9:.1f} {x-8:.1f},{y+6:.1f} {x+8:.1f},{y+6:.1f}'
            fill, sw, tag = "#d92b2b", 1.3, ""
        else:
            poly = f'{x:.1f},{y-5.5:.1f} {x-5:.1f},{y+3:.1f} {x+5:.1f},{y+3:.1f}'
            fill, sw, tag = "#aab2bb", 0.8, "（真值区间外，不计入统计）"
        parts.append(f'<g><title>算法买点{tag} · {esc(str(r["time"])[:16])}</title>'
                     f'<polygon points="{poly}" fill="{fill}" stroke="#fff" stroke-width="{sw}"/></g>')
    for i in d["sell"]:
        if not (w0 <= i <= w1):
            continue
        j = i - w0
        r = sub.iloc[j]
        x, y = X(j), Y(r["high"]) + ALGO_GAP
        y = min(max(y, MT + 10), PANEL_H - MB - 10)
        if eval_lo - 1 <= i <= eval_hi + 1:
            poly = f'{x:.1f},{y+9:.1f} {x-8:.1f},{y-6:.1f} {x+8:.1f},{y-6:.1f}'
            fill, sw, tag = "#12855a", 1.3, ""
        else:
            poly = f'{x:.1f},{y+5.5:.1f} {x-5:.1f},{y-3:.1f} {x+5:.1f},{y-3:.1f}'
            fill, sw, tag = "#aab2bb", 0.8, "（真值区间外，不计入统计）"
        parts.append(f'<g><title>算法卖点{tag} · {esc(str(r["time"])[:16])}</title>'
                     f'<polygon points="{poly}" fill="{fill}" stroke="#fff" stroke-width="{sw}"/></g>')

    # 面板标题与图例
    prec = 100 * d["hit_sig"] / d["n_sig"] if d["n_sig"] else 0
    rec = 100 * d["hit_truth"] / d["n_truth"] if d["n_truth"] else 0
    title = f'{d["name"]} {d["code"][2:]} · {"日线" if d["period"]=="day" else "60 分钟"}'
    fmt = (lambda t: str(t)[:10]) if d["period"] == "day" else (lambda t: str(t)[:16])
    miss = d["n_truth"] - d["hit_truth"]
    fake = d["n_sig"] - d["hit_sig"]
    info = (f'窗口 {fmt(sub["time"].iloc[0])} ~ {fmt(sub["time"].iloc[-1])}　'
            f'真值 {d["n_truth"]} 个 · 算法 {d["n_sig"]} 个　'
            f'命中 {d["hit_sig"]}（±1 根）· 漏 {miss} · 假 {fake}　'
            f'精度 {prec:.0f}% · 覆盖 {rec:.0f}%')

    lx = W - MR - 430
    legend = (
        f'<g transform="translate({lx},{24})">'
        f'<circle cx="7" cy="6" r="6.5" fill="#fff" stroke="#d92b2b" stroke-width="2.4"/>'
        f'<text x="20" y="10" font-size="12" fill="#4b5563">表格真值</text>'
        f'<polygon points="90,0 84,12 96,12" fill="#d92b2b" stroke="#fff" stroke-width="1.2"/>'
        f'<text x="102" y="10" font-size="12" fill="#4b5563">算法信号</text>'
        f'<polygon points="176,2 172,11 180,11" fill="#aab2bb" stroke="#fff" stroke-width="0.8"/>'
        f'<text x="186" y="10" font-size="11.5" fill="#8a9099">区间外·不计分</text>'
        f'<line x1="290" y1="6" x2="308" y2="6" stroke="#2f7ed8" stroke-width="1.8"/>'
        f'<text x="314" y="10" font-size="12" fill="#4b5563">MA7</text>'
        f'</g>')

    return (f'<svg viewBox="0 0 {W} {PANEL_H}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:auto;display:block" '
            f'font-family="PingFang SC, Microsoft YaHei, sans-serif">'
            f'<rect width="{W}" height="{PANEL_H}" fill="#fff"/>'
            f'<text x="{ML}" y="26" font-size="16" font-weight="600" fill="#1a1d21">{esc(title)}</text>'
            f'<text x="{ML}" y="46" font-size="11.5" fill="#8a9099">{esc(info)}</text>'
            f'{legend}' + "".join(parts) + '</svg>')


# ────────────────────────── 主流程 ──────────────────────────
def main():
    cfg = yaml.safe_load(open(ROOT / "config.yaml"))
    truth = {tuple(k.split("|")): v for k, v in json.load(open(CACHE / "truth.json")).items()}
    order = [("sz300251", "day"), ("sz300750", "day"), ("sz302132", "day"),
             ("sz300251", "m60"), ("sz300750", "m60"), ("sz302132", "m60")]

    panels, rows = [], []
    tot = dict(n=0, hit=0, nt=0, ht=0)
    for key in order:
        if key not in truth:
            continue
        d = load_one(key[0], key[1], truth[key], cfg)
        panels.append(render(d))
        p = 100 * d["hit_sig"] / d["n_sig"] if d["n_sig"] else 0
        r = 100 * d["hit_truth"] / d["n_truth"] if d["n_truth"] else 0
        rows.append(dict(name=d["name"], code=d["code"][2:],
                         period="日线" if d["period"] == "day" else "60 分钟",
                         n_sig=d["n_sig"], hit=d["hit_sig"], prec=p,
                         n_t=d["n_truth"], ht=d["hit_truth"], rec=r))
        tot["n"] += d["n_sig"]; tot["hit"] += d["hit_sig"]
        tot["nt"] += d["n_truth"]; tot["ht"] += d["hit_truth"]

    P = 100 * tot["hit"] / tot["n"] if tot["n"] else 0
    R = 100 * tot["ht"] / tot["nt"] if tot["nt"] else 0

    def tr(r):
        return (f'<tr><td>{r["name"]}</td><td class="c">{r["code"]}</td><td class="c">{r["period"]}</td>'
                f'<td class="c">{r["n_sig"]}</td><td class="c">{r["hit"]}</td>'
                f'<td class="c {"hi" if r["prec"]>=60 else ""}">{r["prec"]:.0f}%</td>'
                f'<td class="c">{r["n_t"]}</td><td class="c">{r["ht"]}</td><td class="c">{r["rec"]:.0f}%</td></tr>')

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>真值 vs 算法 · K线标注</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#1a1d21;--dim:#6b7280;--line:#e5e7eb;--red:#d92b2b;--grn:#12855a;--blue:#2f7ed8}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1720px;margin:0 auto;padding:30px 22px 70px}}
h1{{font-size:24px;margin:0 0 4px}}
.sub{{color:var(--dim);font-size:13px;margin-bottom:20px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px;overflow-x:auto}}
h2{{font-size:16px;margin:0 0 6px;padding-left:10px;border-left:4px solid var(--blue)}}
.legendbar{{display:flex;gap:26px;flex-wrap:wrap;font-size:13.5px;color:#4b5563;margin:10px 0 4px;align-items:center}}
.legendbar i{{display:inline-block;vertical-align:middle;margin-right:7px}}
.circ{{width:13px;height:13px;border:2.2px solid #1a1d21;border-radius:50%}}
.tri{{width:0;height:0;border-left:6px solid transparent;border-right:6px solid transparent;border-bottom:11px solid #1a1d21}}
.mline{{width:20px;height:2px;background:#2f7ed8}}
table{{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:8px}}
th{{background:#f0f2f5;text-align:left;padding:8px 10px;font-weight:600;border-bottom:1px solid var(--line);white-space:nowrap}}
td{{padding:7px 10px;border-bottom:1px solid #f1f2f4}}
td.c{{text-align:center}}
td.hi{{color:var(--red);font-weight:600}}
tr.tot td{{font-weight:600;background:#fafbfc}}
.kpi{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:8px 0}}
.kpi div{{background:#fbfbfc;border:1px solid var(--line);border-radius:10px;padding:11px 14px}}
.kpi b{{display:block;font-size:22px}}
.kpi span{{font-size:12px;color:var(--dim)}}
.note{{background:#fff8e6;border:1px solid #f3e0a8;border-radius:9px;padding:11px 14px;font-size:13.5px;margin-top:10px}}
</style></head><body><div class="wrap">
<h1>表格真值 vs 我们算出的信号</h1>
<div class="sub">同一张 K 线图上叠加两套标注 · 真值来自腾讯表格 6 组 61 个点 · 算法为当前 config 的生产输出 · 2026-11-25</div>

<div class="card">
<h2>怎么读这张图</h2>
<div class="legendbar">
  <span><i class="circ"></i>表格真值（空心圆，贴近 K 线）</span>
  <span><i class="tri"></i>算法信号（实心三角，外一层）</span>
  <span><i class="mline"></i>MA7（算法就是按收盘与它穿越判的）</span>
  <span style="color:#8a9099">红=买 / 绿=卖；买标在低点下方，卖标在高点上方；<b>灰色小三角＝真值日期区间之外的历史信号，不计入统计</b></span>
</div>
<div class="kpi">
  <div><b style="color:var(--red)">{tot['n']}</b><span>算法在评估窗口内发出的信号</span></div>
  <div><b style="color:var(--red)">{tot['hit']}</b><span>其中与真值同向且相差 ≤1 根</span></div>
  <div><b>{P:.0f}%</b><span>±1 根精度</span></div>
  <div><b>{R:.0f}%</b><span>真值覆盖率（{tot['ht']}/{tot['nt']}）</span></div>
</div>
<table>
<thead><tr><th>标的</th><th>代码</th><th>周期</th><th>算法信号</th><th>命中</th><th>精度</th><th>真值数</th><th>被覆盖</th><th>召回</th></tr></thead>
<tbody>{''.join(tr(r) for r in rows)}
<tr class="tot"><td colspan="3">合计</td><td class="c">{tot['n']}</td><td class="c">{tot['hit']}</td>
<td class="c">{P:.0f}%</td><td class="c">{tot['nt']}</td><td class="c">{tot['ht']}</td><td class="c">{R:.0f}%</td></tr>
</tbody></table>
<div class="note"><b>看图的重点不是数量对得上，而是位置对得上。</b>圆和三角成对出现在同一根（或紧邻一根）K 线上＝命中；只有圆没有三角＝漏掉；只有三角没有圆＝我们的假信号。价格标注在右轴，十字与网格为 5 等分。</div>
</div>

{''.join(f'<div class="card">{p}</div>' for p in panels)}

</div></body></html>"""

    OUT.write_text(html, encoding="utf-8")
    print("written", OUT, f"{len(html)/1024:.0f}KB")
    for r in rows:
        print(f'  {r["name"]:6s} {r["period"]:6s} 信号{r["n_sig"]:3d} 命中{r["hit"]:3d} 精度{r["prec"]:5.1f}% 召回{r["rec"]:5.1f}%')
    print(f'  合计 精度 {P:.1f}%  召回 {R:.1f}%')


if __name__ == "__main__":
    main()
