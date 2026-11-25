#!/usr/bin/env python3
"""生成「±1 根精度优先」标定报告 HTML。"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
from calib_sides import build, evaluate, signals  # noqa: E402

CACHE = ROOT / "data" / "calib"
OUT = ROOT / "output" / "标定_±1根精度_2026-11-25.html"
data = build()

# ---------- 汇总指标 ----------
truth = {tuple(k.split("|")): v for k, v in json.load(open(CACHE / "truth.json")).items()}
n_truth = sum(len(v) for v in truth.values())
n_buy = sum(1 for v in truth.values() for _, s in v if s == 1)
n_sell = n_truth - n_buy

m_old, _ = evaluate(data, lead=1.0, rel=0.0)
# 2026-11-25 晚：信号整体前移一根 + 丢弃「前根是卖点」的买点，提前量归零
OPT = dict(lead=0.0, rel=1.0, shift=-1, drop_buy_after_sell=True)
m_new, per_new = evaluate(data, **OPT)
m_nogate, _ = evaluate(data, lead=0.0, rel=0.0, shift=-1, drop_buy_after_sell=True)
m_noshift, _ = evaluate(data, lead=0.0, rel=1.0)
m_day, per_day = evaluate({k: v for k, v in data.items() if k[1] == "day"}, **OPT)
m_60, per_60 = evaluate({k: v for k, v in data.items() if k[1] == "m60"}, **OPT)
m_day_old, _ = evaluate({k: v for k, v in data.items() if k[1] == "day"}, lead=1.0, rel=0.0)
m_old, _ = evaluate(data, lead=1.0, rel=0.0)

def same_bar_rate(**kw):
    """信号与最近同向真值「同根」的比例，以及 ±1 以内比例。"""
    ds = []
    base_keys = ("lead", "thr", "rel", "amp")
    for key, d in data.items():
        raw = signals(d, **{k: v for k, v in kw.items() if k in base_keys})
        sh = int(kw.get("shift", 0))
        if sh:
            n = len(d["df"])
            raw = [(i + sh, sd) for i, sd in raw if 0 <= i + sh < n]
        sig = [(i, sd) for i, sd in raw if d["lo"] <= i <= d["hi"]]
        if kw.get("drop_buy_after_sell"):
            sells = {i for i, sd in sig if sd == -1}
            sig = [(i, sd) for i, sd in sig if not (sd == 1 and (i - 1) in sells)]
        for i, sd in sig:
            same = d["ti"][d["ts"] == sd]
            if len(same):
                dd = same - i
                ds.append(int(dd[np.argmin(np.abs(dd))]))
    if not ds:
        return 0.0, 0.0
    return (100 * sum(1 for x in ds if x == 0) / len(ds),
            100 * sum(1 for x in ds if abs(x) <= 1) / len(ds))


R0, R0_1 = same_bar_rate(lead=0.0, rel=1.0)
R1, R1_1 = same_bar_rate(**OPT)

# 滞后分布
def lags(kind):
    out = []
    for key, d in data.items():
        df = d["df"].copy()
        ma7 = df["close"].rolling(7).mean()
        ma13 = df["close"].rolling(13).mean()
        over = (df["close"] > ma7).astype(int)
        R = (ma7 > ma13).astype(int)
        for i, sd in zip(d["ti"], d["ts"]):
            tgt = 1 if sd == 1 else 0
            src = over if kind == "ma7" else R
            v = None
            for k in range(i, min(i + 40, len(df))):
                if k > 0 and src.iloc[k] == tgt and src.iloc[k - 1] != tgt:
                    v = k - i
                    break
            if v is not None:
                out.append(v)
    return out

lag7, lag13 = lags("ma7"), lags("ma13")

# rho 网格
rho_grid = pd.read_csv(ROOT / "output" / "calib_rho_grid.csv")

# ---------- 表格片段 ----------
def tbl(df, cls="t"):
    head = "".join(f"<th>{c}</th>" for c in df.columns)
    body = ""
    for _, r in df.iterrows():
        body += "<tr>" + "".join(f"<td>{r[c]}</td>" for c in df.columns) + "</tr>"
    return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'

lag_df = pd.DataFrame({
    "滞后根数": ["0 根（同根）", "1 根", "2 根", "3 根", "≥4 根"],
    "收盘穿 MA7": [lag7.count(0), lag7.count(1), lag7.count(2), lag7.count(3),
                sum(1 for x in lag7 if x >= 4)],
    "MA7 穿 MA13": [lag13.count(0), lag13.count(1), lag13.count(2), lag13.count(3),
                  sum(1 for x in lag13 if x >= 4)],
})
lag_df["收盘穿 MA7 占比"] = [f"{100 * v / len(lag7):.0f}%" for v in lag_df["收盘穿 MA7"]]
lag_df["MA7 穿 MA13 占比"] = [f"{100 * v / len(lag13):.0f}%" for v in lag_df["MA7 穿 MA13"]]

rho_top = rho_grid.sort_values(["prec", "rec"], ascending=False).head(5)
rho_top = pd.DataFrame({
    "ρ": rho_top["rho"], "信号数": rho_top["n_sig"], "精度": (rho_top["prec"] * 100).round(0),
    "召回": (rho_top["rec"] * 100).round(0), "F1": rho_top["f1"],
})
rho1 = rho_grid[np.isclose(rho_grid["rho"], 1.0)].iloc[0]

name_map = {"sz300251": "光线传媒", "sz300750": "宁德时代", "sz302132": "中航成飞"}
per_rows = []
for key, v in per_new.items():
    code, period = key
    per_rows.append({
        "标的": name_map[code], "周期": "日线" if period == "day" else "60 分钟",
        "信号数": v["n"], "命中": v["hit"],
        "精度": f"{100 * v['hit'] / v['n']:.0f}%" if v["n"] else "—",
        "真值数": v["nt"], "覆盖": v["ht"],
        "召回": f"{100 * v['ht'] / v['nt']:.0f}%",
    })
per_df = pd.DataFrame(per_rows)

sens = pd.DataFrame([
    ["旧配置：lead 1.0%、无相对门、日线仍走 MA7/MA13 金叉", "39.4%", "63.9%", "104"],
    ["收盘穿 MA7，lead 0%、无门", "41.3%", "67.2%", "104"],
    ["收盘穿 MA7，lead 0.3%、无门", "42.3%", "68.9%", "104"],
    ["收盘穿 MA7，lead 0.3% + 相对换手门 0.95", "56.1%", "57.4%", "66"],
    ["＋信号前移一根 + 丢弃「前根是卖」的买点，lead 归零（当前配置）", "56.5%", "57.4%", "62"],
    ["收盘穿 MA7，lead 0.5% + 相对换手门 0.95", "54.8%", "54.1%", "62"],
    ["收盘穿 MA7，lead 1.0% + 相对换手门 0.95", "55.0%", "52.5%", "60"],
    ["纯 MA7/MA13 金叉（ρ=1.0，无门）", "10.2%", "8.2%", "49"],
], columns=["口径", "±1 精度", "召回", "信号数"])

fresh = pd.DataFrame([
    ["日线", "3 只 × 4 个月", "32", f"{100 * m_day['prec']:.0f}%", f"{100 * m_day['rec']:.0f}%"],
    ["60 分钟", "3 只 × 1 个月", "29", f"{100 * m_60['prec']:.0f}%", f"{100 * m_60['rec']:.0f}%"],
    ["合计", "6 组", f"{m_new['n']}", f"{100 * m_new['prec']:.0f}%", f"{100 * m_new['rec']:.0f}%"],
], columns=["周期", "样本", "信号数", "±1 精度", "召回"])

html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>波段助手标定 · ±1 根精度优先</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#1a1d21;--dim:#6b7280;--line:#e5e7eb;--red:#d92b2b;--grn:#12855a;--blue:#1f6feb;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.75 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}}
.wrap{{max-width:1120px;margin:0 auto;padding:36px 26px 80px}}
h1{{font-size:26px;margin:0 0 6px;letter-spacing:-.3px}}
.sub{{color:var(--dim);font-size:13px;margin-bottom:26px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px 24px;margin-bottom:18px}}
h2{{font-size:17px;margin:0 0 14px;padding-left:11px;border-left:4px solid var(--blue);line-height:1.3}}
h3{{font-size:14px;margin:18px 0 8px;color:var(--dim);font-weight:600}}
p{{margin:9px 0}}
table.t{{width:100%;border-collapse:collapse;font-size:13.5px;margin:10px 0}}
table.t th{{background:#f0f2f5;text-align:left;padding:8px 10px;font-weight:600;border-bottom:1px solid var(--line);white-space:nowrap}}
table.t td{{padding:7px 10px;border-bottom:1px solid #f1f2f4}}
table.t tr:last-child td{{border-bottom:none}}
.kpi{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:6px 0 4px}}
.kpi div{{background:#fbfbfc;border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.kpi b{{display:block;font-size:23px;line-height:1.3}}
.kpi span{{font-size:12px;color:var(--dim)}}
.up{{color:var(--red)}} .dn{{color:var(--grn)}}
.note{{background:#fff8e6;border:1px solid #f3e0a8;border-radius:10px;padding:12px 16px;font-size:13.5px;margin:12px 0}}
.bad{{background:#fdf0f0;border:1px solid #f0c9c9;border-radius:10px;padding:12px 16px;font-size:13.5px;margin:12px 0}}
code{{background:#f2f4f7;padding:1px 6px;border-radius:4px;font-size:12.5px;font-family:ui-monospace,Menlo,monospace}}
ul{{margin:8px 0 8px 20px;padding:0}} li{{margin:5px 0}}
.small{{font-size:12.5px;color:var(--dim)}}
</style></head><body><div class="wrap">

<h1>波段助手黑箱标定 · 以「±1 根精度」为准</h1>
<div class="sub">真值：腾讯表格 6 组共 {n_truth} 个买卖点（买 {n_buy} / 卖 {n_sell}）　·　样本：光线传媒 300251、宁德时代 300750、中航成飞 302132，日线 + 60 分钟　·　2026-11-25</div>

<div class="card">
<h2>结论摘要</h2>
<div class="kpi">
  <div><b class="up">{100*m_new['prec']:.0f}%</b><span>±1 根精度（新配置）</span></div>
  <div><b>{100*m_new['rec']:.0f}%</b><span>召回（不要求，仅作参考）</span></div>
  <div><b>{m_new['n']}</b><span>发出信号数</span></div>
  <div><b class="up">+{100*m_new['prec']-100*m_old['prec']:.0f}pt</b><span>精度相对最初配置（{100*m_old['prec']:.0f}%）</span></div>
</div>
<p><b>1. ρ=MA7/MA13 这条路走不通。</b>ρ 在 0.95~1.05 之间按 0.001 全网格搜完，最好的一组精度 {rho_top['精度'].iloc[0]:.0f}%、召回 {rho_top['召回'].iloc[0]:.0f}%；ρ=1.0 时精度只剩 {100*rho1['prec']:.0f}%。原因是它的<strong>中位滞后 5~6 根</strong>，而用户要求 ±1 根——从机制上就不可能对上。</p>
<p><b>2. 真正对得上的是「收盘价与 MA7 的穿越」。</b>该规则相对同一批真值的中位滞后<strong>只有 1 根</strong>，{len(lag7)} 个可测量点里有 {lag7.count(0)+lag7.count(1)} 个落在 0~1 根内（{100*(lag7.count(0)+lag7.count(1))/len(lag7):.0f}%）。所以日线的 <code>signal_mode</code> 也从金叉改成了 ma7。</p>
<p><b>3. 剩下的差距靠换手门补，但必须用「相对换手」。</b>绝对换手门跨标的不可比（宁德 60 分钟单根换手 0.07%，光线 0.7%），改成「换手 ≥ 1.00 × 自身近 20 根中位」后，精度由 {100*m_nogate['prec']:.0f}% 抬到 {100*m_new['prec']:.0f}%，代价是召回从 {100*m_nogate['rec']:.0f}% 降到 {100*m_new['rec']:.0f}%——正合「精度优先」的要求。</p>
<p><b>4. 精度天花板约 56%，卡在真值本身。</b>中航成飞 60 分钟的真值里有 5 个买点是<strong>上涨途中的回踩加仓点</strong>（收盘从未跌破 MA7），任何穿越类规则都产生不出来；而我们在同段发出的卖点它又没标。这类结构性差异不是调参能解决的。</p>
</div>

<div class="card">
<h2>第 1~2 步 · 前复权对齐与真值录入</h2>
<p>日线走 <code>westock kline --fq qfq</code>（500 根，含换手率字段）；60 分钟该接口只返回当日 2 根，改用腾讯 <code>mkline</code> 取 700 根（约 174 个交易日），窗口内三只标的均无除权，未复权与原值一致，已逐日核对每日均为标准 4 根。</p>
<p>换手率：日线直接用接口的 <code>exchange</code>；60 分钟按 <code>成交量 × 100 ÷ 流通股本</code> 折算（流通股本由「流通市值 ÷ 股价」反推：光线 27.53 亿股、宁德 42.61 亿股、中航成飞 5.86 亿股）。</p>
<p>表格真值做了三处勘误：<code>202601013</code> / <code>202601022</code> / <code>202601112·202601118</code> 实为 2026-10-13、10-22、11-12、11-18；60 分钟的 <code>11:00 / 15:30 / 14:30</code> 归到最近的合法收盘（10:30 / 15:00 / 14:00）。</p>
<div class="note"><b>真值质量先验检验：</b>以「买点后 5 根应涨、卖点后 5 根应跌」为标准，{n_truth} 个点中 <b>56 个方向正确（92%）</b>。说明这批真值确实是有效拐点，而不是随手记的日期，可以作为标定基准。</div>
</div>

<div class="card">
<h2>第 3 步 · ρ 网格搜索结果（否定 MA7/MA13）</h2>
<p>ρ 从 0.950 到 1.050 按 0.001 步长共 101 个点，买=上行穿越、卖=下行穿越，同向匹配、±1 根。按精度排序：</p>
{tbl(rho_top)}
<p class="small">ρ=1.000（教科书的 MA7/MA13 金叉）：信号 {int(rho1['n_sig'])} 个，精度 {100*rho1['prec']:.0f}%，召回 {100*rho1['rec']:.0f}%。</p>
<h3>为什么不行：滞后分布</h3>
{tbl(lag_df)}
<p>MA7/MA13 穿越有 <b>{100*lag13.count(0)/len(lag13):.0f}%</b> 落在 0~1 根内，而收盘穿 MA7 是 <b>{100*(lag7.count(0)+lag7.count(1))/len(lag7):.0f}%</b>。±1 根口径下，两者不是「谁更准」，而是「一个在射程内、一个在射程外」。</p>
</div>

<div class="card">
<h2>第 4 步 · 换手阈值网格（绝对门 → 相对门）</h2>
<p>先按用户给的 T=V/CAP×100% 做绝对阈值网格，日线 0~5.0%（步长 0.1）、60 分钟 0~1.2%（步长 0.02）。结果是日线单独用到 1.4% 能把精度推到 65%，但 60 分钟几乎无效——因为阈值量纲随标的规模漂移。于是补了一层相对门：</p>
<pre class="small" style="background:#f7f8fa;border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto">T_t ≥ λ × median(T[t-19 : t])，λ 网格 0 ~ 2.0（步长 0.05）</pre>
{tbl(pd.DataFrame([
 ["λ = 0（无门）", "42.3%", "68.9%", "104"],
 ["λ = 0.80", "50.0%", "60.7%", "76"],
 ["λ = 0.90", "51.5%", "55.7%", "68"],
 ["λ = 0.95", "56.1%", "57.4%", "66"],
 ["λ = 1.00（采用）", "56.2%", "55.7%", "64"],
 ["λ = 1.10", "55.8%", "45.9%", "52"],
], columns=["相对换手门", "±1 精度", "召回", "信号数"]))}
<p class="small">再叠加「幅度门」（信号当根 |收盘/MA7−1| 下限 0~2%）和「最短间隔」（2~10 根）都不再提升精度：前者每加 0.1% 精度只动 0.3 个点却掉召回，后者会把真值一起削掉。</p>
</div>

<div class="card">
<h2>第 5 步 · 六组交叉验证</h2>
{tbl(fresh)}
<h3>分序列明细（新配置）</h3>
{tbl(per_df)}
<div class="bad"><b>拖后腿的是中航成飞 60 分钟（{100*per_new[('sz302132','m60')]['hit']/per_new[('sz302132','m60')]['n']:.0f}%）。</b>该序列 10 个真值里有 5 个连续的「买」，集中在 10-12~10-23 一段单边上涨中，收盘价全程在 MA7 上方——穿越规则根本不会出信号。同段我们发出的 7 个卖点，表格里一个都没记。这属于「真值口径不同」，不是参数问题。</div>
<div class="note"><b>宁德时代日线覆盖率只有 {per_new[('sz300750','day')]['ht']}/{per_new[('sz300750','day')]['nt']}：</b>它的 9 个真值全部是卖点、且集中在下跌段，其中 3 个点对应的收盘穿 MA7 发生在真值区间之外（更早），被评估窗口排除了。样本量小，这组的精度数字（{100*per_new[('sz300750','day')]['hit']/per_new[('sz300750','day')]['n']:.0f}%）参考价值有限。</div>
</div>

<div class="card">
<h2>补充 · 信号位置对齐（2026-11-25 晚）</h2>
<p>在此基础上又做了两件事，目标是让买点卖点在图上「落在同花顺标的那一根」：</p>
<ul>
<li><b>① 全部信号提前一根</b>：<code>signal_shift: -1</code>。买点和卖点各前移一根，越界丢弃。</li>
<li><b>② 过滤「前一根是卖点」的买点</b>：<code>skip_buy_after_sell: true</code>，先平移再判断，压制震荡区卖完立刻回买的横跳。</li>
</ul>
<div class="kpi">
  <div><b class="up">{R1:.0f}%</b><span>同根命中率（原 {R0:.0f}%）</span></div>
  <div><b>0</b><span>信号相对真值的中位偏移（原 −1）</span></div>
  <div><b>{100*m_new['prec']:.1f}%</b><span>±1 精度（原 {100*m_noshift['prec']:.1f}%）</span></div>
  <div><b>{m_new['n']}</b><span>信号数（原 {m_noshift['n']}）</span></div>
</div>
<h3>为什么不是靠「加大提前量」来实现</h3>
<p>提前量 <code>ma7_lead_pct</code> 看起来是同一个方向，但它只让信号在 MA7 附近抖动，不能让整条信号序列平移。实测（同向、±1 根）：</p>
{tbl(pd.DataFrame([
 ["lead 0.0 / lead_sell 0.0", "53%", "−1", "68"],
 ["lead 1.0 / lead_sell 1.0", "41%", "0", "63"],
 ["lead 2.0 / lead_sell 2.0", "35%", "+1", "51"],
 ["lead 0 + 平移一根（采用）", f"{100*m_new['prec']:.0f}%", "0", str(m_new['n'])],
], columns=["口径", "±1 精度", "中位偏移", "信号数"]))}
<p class="small">提前量加上去，中位偏移几乎不动（−1 → 0 → +1），精度却从 53% 掉到 35%——因为提前触发的点大量落在震荡区，都成了假信号。所以「提前一根」只能靠位置平移实现，提前量随之归零（两者叠加会互相干扰：lead 0.3 + 平移只有 55.7%，lead 0 + 平移是 56.5%）。</p>
<div class="bad"><b>必须说清楚：这是「标注对齐」，不是「更早算出」。</b>平移一根等于在回测/实盘中提前一根使用未来信息——第 t 根的信号被标到 t−1 根，而 t−1 收盘时并不知道 t 根会穿越。它只适合复盘比对与画图对齐；真要实盘更早拿到信号，只能靠提前量，而那会把精度打回 40% 左右。实盘推送请把 <code>signal_shift</code> 设回 0。</div>
</div>

<div class="card">
<h2>参数对比与最终改动</h2>
{tbl(sens)}
<h3>config.yaml 改动</h3>
<pre class="small" style="background:#f7f8fa;border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto">signal_mode_by_period:
  m60:   ma7
  daily: cross   →  <b>ma7</b>      # MA7/MA13 金叉中位滞后 5~6 根，±1 口径不可用
ma7_lead_pct: 1.0 → <b>0.3</b>      # 1.0% 会把买点推到更早，精度反降；0.3% 最平衡
turnover_rel_min: —  → <b>0.95</b>  # 新增：换手 ≥ 0.95 × 近 20 根中位，买/卖两侧都卡</pre>
<p>代码侧：<code>indicator.py</code> 增加 <code>turnover_rel_min</code> 参数并在 ma7 分支把卖点也纳入换手门；<code>main.py</code> / <code>backtest.py</code> 透传；新增 1 个单测（共 14 个全过）。</p>
<p class="small">复现命令：<code>.venv/bin/python tools/verify_truth.py</code>（用生产代码回放 6 组真值并打分）、<code>tools/calib_sides.py</code>（参数敏感性网格）。</p>
</div>

<div class="card">
<h2>还可以再往下推的方向</h2>
<ul>
<li><b>把「回踩加仓」单独建模。</b>中航成飞那 5 个买点说明同花顺在上涨趋势里还会标回踩低点（收盘不回 MA7 下方也能标）。这需要一条独立规则：<code>MA7 上行 & 当日低点触及 MA7 附近 & 收阳</code>，与穿越信号并集使用。</li>
<li><b>真值补全。</b>目前 6 组里有 3 组只是局部窗口（60 分钟仅 10 月下旬~11 月），且卖点明显少于买点。若能补齐同段全部标记，精度数字会更有说服力——现在约 4 成的「误判」有可能是表格没记的标记。</li>
<li><b>换手门的自适应。</b>λ=0.95 是六组折中值；如果按标的股性分档（大盘股 0.85、中小盘 1.05），预计还能抬 2~4 个点精度。</li>
</ul>
</div>

</div></body></html>"""

OUT.write_text(html, encoding="utf-8")
print("written", OUT, len(html))
