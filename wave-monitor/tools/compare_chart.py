#!/usr/bin/env python3
"""生成「同花顺波段助手」口径对比图（自绘 SVG，无第三方绘图依赖）。

用法：
    python3 tools/compare_chart.py --code 300251 --period m60 \
        --out output/光线传媒_60m_信号口径对比.html

图面三段：
  1) 价格 + MA7/MA13 + 旧口径（rho 金叉）买卖点
  2) 波段助手复刻：橙色=收盘站上 MA7，蓝色=跌破 MA7，买/卖标在切换那根
  3) 底部标注 11/23 11:30 这类「同花顺标了买、金叉口径没标」的差异点
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fetcher import MarketFetcher  # noqa: E402
from indicator import compute_indicators  # noqa: E402
from utils import load_config, setup_logging  # noqa: E402

RED = "#e8352b"      # 涨
GREEN = "#12a05a"    # 跌
ORANGE = "#f5921f"   # 持有
BLUE = "#2f80ed"     # 观望
GRID = "#e9e9e9"
TEXT = "#333"


def fetch(code: str, period: str, bars: int, refresh: bool = False) -> tuple[pd.DataFrame, str]:
    """优先读本地缓存（westock 有服务限频，反复取数会被挡）。"""
    cache = ROOT / "data" / "cache" / f"{code}_{period}.csv"
    if cache.exists() and not refresh:
        df = pd.read_csv(cache, parse_dates=["time"])
        if len(df) >= bars * 0.5:
            return df, "本地缓存"

    cfg = load_config(ROOT / "config.yaml")
    cfg.setdefault("fetch", {})["history_bars"] = bars
    cfg.setdefault("general", {})["log_level"] = "WARNING"
    logger = setup_logging(cfg)
    df, source = MarketFetcher(cfg, logger).fetch(code, period)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache, index=False)
    except OSError:
        pass
    return df, source


def lag_stats(df: pd.DataFrame, mask) -> tuple[int, float, int]:
    """买点相对「近 6 根最低点」的滞后：中位根数 + 3 根内跌破买价 2% 的个数。"""
    import numpy as np
    lo = df["low"].values
    lags, fake = [], 0
    for i in np.where(mask)[0]:
        if i < 6:
            continue
        seg = lo[max(0, i - 6):i + 1]
        lag = i - (max(0, i - 6) + int(np.argmin(seg)))
        lags.append(lag)
        fwd = df["close"].iloc[i + 1:i + 4]
        if len(fwd) and fwd.min() < df["close"].iloc[i] * 0.98:
            fake += 1
    return (float(np.median(lags)) if lags else -1), (sum(lags) / len(lags) if lags else -1), fake


def build(code: str, name: str, period: str, df: pd.DataFrame, source: str,
          rho: float, rho_sell: float, thr: float, lead: float = 0.0) -> str:
    old = compute_indicators(df, rho=rho, rho_sell=rho_sell,
                             turnover_threshold=thr, require_bullish=True,
                             signal_mode="cross")
    new = compute_indicators(df, signal_mode="ma7", turnover_threshold=thr,
                             ma7_lead_pct=lead)

    n = len(df)
    W, H = 1680, 962
    L, R = 96, 1620
    step = (R - L) / max(n, 1)
    bw = max(3.0, step * 0.62)

    price_top, price_bot = 92, 536
    panel_top, panel_bot = 660, 800

    lo = float(min(df["low"].min(), old["ma13"].min()))
    hi = float(max(df["high"].max(), old["ma13"].max()))
    pad = (hi - lo) * 0.06
    lo, hi = lo - pad, hi + pad

    def x(i: int) -> float:
        return L + step * (i + 0.5)

    def yp(v: float) -> float:
        return price_bot - (v - lo) / (hi - lo) * (price_bot - price_top)

    def ybar(flag: bool, i: int) -> float:
        """柱高按「偏离 MA7 的幅度」归一，纯示意，与同花顺柱高语义无关。"""
        dev = abs(float(new.loc[i, "close"]) - float(new.loc[i, "ma7"]))
        scale = float((df["close"] - new["ma7"]).abs().max() or 1)
        h = 14 + 96 * min(dev / scale, 1.0)
        return panel_bot - h

    out: list[str] = []
    add = out.append
    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" style="background:#fff;font-family:PingFang SC,Arial,sans-serif">')
    add(f'<rect width="{W}" height="{H}" fill="#fff"/>')
    add(f'<text x="{L}" y="36" font-size="22" font-weight="bold" fill="#222">'
        f'{name} {code} · {"60分钟" if period == "m60" else "日线"} · '
        f'信号口径对比（价格收复 MA7 vs MA7/MA13 金叉）</text>')
    add(f'<text x="{L}" y="60" font-size="13" fill="#666">数据源 {source} | 旧口径 rho={rho} / '
        f'rho_sell={rho_sell} / cross 模式 / 换手≥{thr}% ｜ 新口径 ma7（收盘 vs MA7，'
        f'提前量 {lead * 100:.1f}%）｜ {n} 根</text>')

    # 价格网格
    for k in range(6):
        v = lo + (hi - lo) * k / 5
        y = yp(v)
        add(f'<line x1="{L}" y1="{y:.1f}" x2="{R}" y2="{y:.1f}" stroke="{GRID}"/>')
        add(f'<text x="{L - 10}" y="{y + 4:.1f}" font-size="12" fill="#666" '
            f'text-anchor="end">{v:.2f}</text>')

    # K 线
    for i in range(n):
        o, c, h, l = (float(df.loc[i, k]) for k in ("open", "close", "high", "low"))
        col = RED if c >= o else GREEN
        add(f'<line x1="{x(i):.1f}" y1="{yp(h):.1f}" x2="{x(i):.1f}" y2="{yp(l):.1f}" '
            f'stroke="{col}" stroke-width="1"/>')
        top, bot = yp(max(o, c)), yp(min(o, c))
        add(f'<rect x="{x(i) - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" '
            f'height="{max(bot - top, 1):.1f}" fill="{col}"/>')

    # 均线
    for col, color, label in (("ma7", "#e8a33d", "MA7"), ("ma13", "#3f6fd8", "MA13")):
        pts = " ".join(f"{x(i):.1f},{yp(float(old.loc[i, col])):.1f}"
                       for i in range(n) if pd.notna(old.loc[i, col]))
        add(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6" opacity="0.9"/>')
    add(f'<text x="{L}" y="{price_bot + 22}" font-size="12" fill="#e8a33d">— MA7</text>')
    add(f'<text x="{L + 62}" y="{price_bot + 22}" font-size="12" fill="#3f6fd8">— MA13</text>')
    add(f'<text x="{L + 150}" y="{price_bot + 22}" font-size="12" fill="{RED}">'
        f'▲ 旧口径买（rho 金叉）</text>')
    add(f'<text x="{L + 330}" y="{price_bot + 22}" font-size="12" fill="{GREEN}">'
        f'▼ 旧口径卖（rho 死叉）</text>')

    # 旧口径买卖点（画在价格图上方/下方三角）
    for i in range(n):
        if bool(old.loc[i, "buy"]):
            add(f'<path d="M{x(i):.1f},{yp(float(df.loc[i, "low"])) + 6:.1f} l-6,12 l12,0 z" fill="{RED}"/>')
        if bool(old.loc[i, "sell"]):
            add(f'<path d="M{x(i):.1f},{yp(float(df.loc[i, "high"])) - 6:.1f} l-6,-12 l12,0 z" fill="{GREEN}"/>')

    # 波段助手面板
    add(f'<text x="{L}" y="{panel_top - 60}" font-size="14" fill="{TEXT}">'
        f'波段助手复刻（橙=收盘站上 MA7，蓝=其下；「买」=站上快线，「↑买」=提前量触发仍在蓝柱）</text>')
    add(f'<line x1="{L}" y1="{panel_bot}" x2="{R}" y2="{panel_bot}" stroke="#ddd"/>')
    for i in range(n):
        base = ybar(bool(new.loc[i, "above"]), i)
        col = ORANGE if bool(new.loc[i, "above"]) else BLUE
        add(f'<rect x="{x(i) - bw / 2:.1f}" y="{base:.1f}" width="{bw:.1f}" '
            f'height="{panel_bot - base:.1f}" fill="{col}" rx="1"/>')
    for i in range(n):
        if bool(new.loc[i, "buy"]):
            early = "" if bool(new.loc[i, "above"]) else "↑"
            add(f'<text x="{x(i):.1f}" y="{panel_bot + 17}" font-size="13" font-weight="bold" '
                f'fill="{RED}" text-anchor="middle">{early}买</text>')
        if bool(new.loc[i, "sell"]):
            add(f'<text x="{x(i):.1f}" y="{panel_top - 8}" font-size="13" font-weight="bold" '
                f'fill="{GREEN}" text-anchor="middle">卖</text>')

    # 时间轴
    for i in range(0, n, max(1, n // 12)):
        add(f'<text x="{x(i):.1f}" y="{panel_bot + 46}" font-size="11" fill="#888" '
            f'text-anchor="middle">{df.loc[i, "time"]:%m-%d %H:%M}</text>')

    # 差异标注：同花顺标买、金叉口径未标的点
    diff = [i for i in range(n)
            if bool(new.loc[i, "buy"]) and not bool(old.loc[i, "buy"])]
    med_o, mean_o, fake_o = lag_stats(df, old["buy"].values)
    med_n, mean_n, fake_n = lag_stats(df, new["buy"].values)
    add(f'<text x="{L}" y="{H - 28}" font-size="13" fill="#444">'
        f'旧口径买 {int(old["buy"].sum())} 个（中位滞后 {med_o:.0f} 根）/ 卖 {int(old["sell"].sum())} 个；'
        f'新口径买 {int(new["buy"].sum())} 个（中位滞后 {med_n:.0f} 根，均值 {mean_n:.1f}，'
        f'3 根内破 2% 的 {fake_n} 个）/ 卖 {int(new["sell"].sum())} 个</text>')
    for i in diff[-3:]:
        add(f'<line x1="{x(i):.1f}" y1="{panel_top - 26}" x2="{x(i):.1f}" y2="{panel_bot}" '
            f'stroke="{RED}" stroke-dasharray="4,3" opacity="0.55"/>')
        add(f'<text x="{x(i):.1f}" y="{panel_bot + 62}" font-size="11" fill="{RED}" '
            f'text-anchor="middle">{df.loc[i, "time"]:%m-%d %H:%M}</text>')

    # 重点标注：最近一个「新口径买、旧口径没买」的转折点
    if diff:
        i = diff[-1]
        add(f'<rect x="{x(i) - step / 2:.1f}" y="{price_top}" width="{step:.1f}" '
            f'height="{panel_bot - price_top}" fill="#ffe9a8" opacity="0.35"/>')
        note = (f'{new.loc[i, "time"]:%m-%d %H:%M} 收盘 {new.loc[i, "close"]:.2f} 上穿 '
                f'MA7 {new.loc[i, "ma7"]:.2f} → 标「买」；此时 MA7/MA13 尚未金叉'
                f'（比值 {new.loc[i, "ratio"]:.4f} < rho {rho}）')
        anchor = "end" if x(i) > (L + R) / 2 else "start"
        add(f'<text x="{x(i):.1f}" y="{H - 62}" font-size="12" fill="#b26a00" '
            f'text-anchor="{anchor}">{note}</text>')

    add("</svg>")
    return f'<!DOCTYPE html><html><head><meta charset="utf-8">' \
           f'<title>{name}{period} 信号口径对比</title></head><body style="margin:0">' \
           + "".join(out) + "</body></html>"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--period", default="m60", choices=["m60", "daily"])
    ap.add_argument("--bars", type=int, default=120)
    ap.add_argument("--rho", type=float, default=0.994)
    ap.add_argument("--rho-sell", type=float, default=1.0)
    ap.add_argument("--thr", type=float, default=0.1)
    ap.add_argument("--lead", type=float, default=1.0, help="ma7 买点提前量，单位 %")
    ap.add_argument("--refresh", action="store_true", help="忽略本地缓存重新取数")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    df, source = fetch(args.code, args.period, args.bars, args.refresh)
    html = build(args.code, args.name or args.code, args.period, df, source,
                 args.rho, args.rho_sell, args.thr, args.lead / 100.0)
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"已生成 {out}（{len(df)} 根，来源 {source}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
