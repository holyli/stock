#!/usr/bin/env python3
"""修正版标定：信号只与同向真值匹配（买点 vs 买点、卖点 vs 卖点），±1 根。"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "calib"
FS = {"sz300251": 27.53e8, "sz300750": 42.61e8, "sz302132": 5.86e8}


def idx_of(df, ts, period):
    if period == "day":
        return int(df.index[df["time"].str[:10] == ts][0])
    d, t = ts.split(" ")
    hh, mm = map(int, t.split(":"))
    m = hh * 60 + mm
    best = min([630, 690, 840, 900], key=lambda c: abs(c - m))
    lab = f"{best // 60:02d}:{best % 60:02d}:00"
    return int(df.index[(df["time"].str[:10] == d) & (df["time"].str[11:] == lab)][0])


def build():
    truth = {tuple(k.split("|")): v for k, v in json.load(open(CACHE / "truth.json")).items()}
    out = {}
    for (code, period), tv in truth.items():
        df = pd.read_csv(CACHE / f"{code}_{period}.csv")
        df["ma7"] = df["close"].rolling(7).mean()
        if period == "m60":
            df["turnover"] = df["volume"] * 100.0 / FS[code] * 100.0
        ti = np.array([idx_of(df, t, period) for t, s in tv])
        ts = np.array([s for t, s in tv])
        out[(code, period)] = dict(df=df, ti=ti, ts=ts, lo=ti.min() - 1, hi=ti.max() + 1)
    return out


def signals(d, lead=0.0, thr=0.0, rel=0.0, amp=0.0):
    df = d["df"]
    C = df["close"].to_numpy(); M = df["ma7"].to_numpy(); T = df["turnover"].to_numpy()
    med = pd.Series(T).rolling(20, min_periods=5).median().bfill().ffill().to_numpy()
    A = np.abs(C / M - 1) * 100
    bad = np.isnan(M)
    band = np.where(bad, True, C > M * (1 - lead / 100.0))
    over = np.where(bad, True, C > M)
    pb = np.r_[True, band[:-1]]; po = np.r_[True, over[:-1]]
    up = np.where(band & ~pb)[0]; dn = np.where(~over & po)[0]
    keep = []
    for i, sd in [(i, 1) for i in up] + [(i, -1) for i in dn]:
        if bad[i]:
            continue
        if thr and T[i] < thr:
            continue
        if rel and T[i] < rel * med[i]:
            continue
        if amp and A[i] < amp:
            continue
        keep.append((i, sd))
    return keep


def evaluate(data, shift=0, drop_buy_after_sell=False,
             min_close_pos=None, require_bull=False, min_cross_pct=None, **kw):
    """shift: 信号整体平移的根数（-1=提前一根）；drop_buy_after_sell: 丢弃前一根是卖点的买点。

    min_close_pos: 买点当根「收盘价在振幅中的位置」下限，(close-low)/(high-low)。
                   None=不过滤；0.5=收盘在上半区。只作用于买点，卖点不动。
    require_bull:  买点要求收阳（close>open）。同样只作用于买点。
    """
    tot = dict(n=0, hit=0, nt=0, ht=0)
    per = {}
    for key, d in data.items():
        raw = signals(d, **kw)
        if shift:
            n = len(d["df"])
            raw = [(i + shift, sd) for i, sd in raw if 0 <= i + shift < n]
        sig = [(i, sd) for i, sd in raw if d["lo"] <= i <= d["hi"]]
        if drop_buy_after_sell:
            sells = {i for i, sd in sig if sd == -1}
            sig = [(i, sd) for i, sd in sig if not (sd == 1 and (i - 1) in sells)]
        if min_cross_pct is not None:
            df = d["df"]
            sig = [(i, sd) for i, sd in sig
                   if sd != 1 or (float(df["ma7"].iloc[i]) and
                                  (float(df["close"].iloc[i]) / float(df["ma7"].iloc[i]) - 1) * 100 >= min_cross_pct)]
        if min_close_pos is not None or require_bull:
            df = d["df"]
            kept = []
            for i, sd in sig:
                if sd == 1:
                    o, c = float(df["open"].iloc[i]), float(df["close"].iloc[i])
                    hi, lo = float(df["high"].iloc[i]), float(df["low"].iloc[i])
                    if require_bull and c <= o:
                        continue
                    if min_close_pos is not None:
                        pos = (c - lo) / (hi - lo) if hi > lo else 0.5
                        if pos < min_close_pos:
                            continue
                kept.append((i, sd))
            sig = kept
        ti, ts = d["ti"], d["ts"]
        hs = 0
        for i, sd in sig:
            same = ti[ts == sd]
            if len(same) and np.min(np.abs(same - i)) <= 1:
                hs += 1
        ht = 0
        for i, sd in zip(ti, ts):
            same = [j for j, s2 in sig if s2 == sd]
            if same and np.min(np.abs(np.array(same) - i)) <= 1:
                ht += 1
        per[key] = dict(n=len(sig), hit=hs, nt=len(ti), ht=ht)
        for f, v in [("n", len(sig)), ("hit", hs), ("nt", len(ti)), ("ht", ht)]:
            tot[f] += v
    p = tot["hit"] / tot["n"] if tot["n"] else 0
    r = tot["ht"] / tot["nt"]
    return dict(prec=round(p, 3), rec=round(r, 3), n=tot["n"],
                f1=round(2 * p * r / (p + r), 3) if p + r else 0), per


if __name__ == "__main__":
    data = build()
    print("基准（无门）：", evaluate(data)[0])
    print("\n提前量")
    for lead in [0, 0.3, 0.5, 0.8, 1.0, 1.5]:
        print("  lead=%.1f  %s" % (lead, evaluate(data, lead=lead)[0]))
    print("\n相对换手门（lead=0.5）")
    for rel in [0, 0.8, 0.9, 0.95, 1.0, 1.1]:
        print("  rel=%.2f  %s" % (rel, evaluate(data, lead=0.5, rel=rel)[0]))
    print("\n日线绝对换手门（lead=0.5, rel=0.95）")
    day = {k: v for k, v in data.items() if k[1] == "day"}
    for thr in [0, 0.5, 0.8, 1.0, 1.2, 1.4]:
        print("  thr=%.1f  %s" % (thr, evaluate(day, lead=0.5, rel=0.95, thr=thr)[0]))
    print("\n幅度门（lead=0.5, rel=0.95）")
    for amp in [0, 0.2, 0.3, 0.5, 0.8]:
        print("  amp=%.1f  %s" % (amp, evaluate(data, lead=0.5, rel=0.95, amp=amp)[0]))
