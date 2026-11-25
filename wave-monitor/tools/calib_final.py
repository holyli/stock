#!/usr/bin/env python3
"""5 步黑箱标定（精度优先版）

规则：收盘穿越 MA7（买=上穿，卖=下穿），信号打穿越当根。
命中判定：与真值偏差 <= 1 根 K 线。
第 4 步：对换手阈值 T=V/CAP*100% 与幅度阈值做网格搜索，追求精度而非召回。
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "calib"
FLOAT_SHARES = {"sz300251": 27.53e8, "sz300750": 42.61e8, "sz302132": 5.86e8}


def idx_of(df, ts, period):
    if period == "day":
        return int(df.index[df["time"].str[:10] == ts][0])
    d, t = ts.split(" ")
    hh, mm = map(int, t.split(":"))
    m = hh * 60 + mm
    best = min([630, 690, 840, 900], key=lambda c: abs(c - m))
    lab = f"{best // 60:02d}:{best % 60:02d}:00"
    return int(df.index[(df["time"].str[:10] == d) & (df["time"].str[11:] == lab)][0])


def build(truth):
    out = {}
    for (code, period), tv in truth.items():
        df = pd.read_csv(CACHE / f"{code}_{period}.csv")
        df["ma7"] = df["close"].rolling(7).mean()
        if period == "m60":
            df["turnover"] = df["volume"] * 100.0 / FLOAT_SHARES[code] * 100.0
        over = (df["close"] > df["ma7"]).fillna(False).astype(bool)
        prev = over.shift(1).fillna(False).astype(bool)
        up = np.where((over & ~prev).values)[0]
        dn = np.where((~over & prev).values)[0]
        ti = np.array([idx_of(df, t, period) for t, s in tv])
        ts = np.array([s for t, s in tv])
        lo, hi = ti.min() - 1, ti.max() + 1
        out[(code, period)] = dict(df=df, up=up, dn=dn, ti=ti, ts=ts, lo=lo, hi=hi)
    return out


def evaluate(data, thr=0.0, amp=0.0, rel=0.0, gate_sell=True, tol=1):
    """返回 各期精度；thr 绝对换手阈值(%)，rel 相对阈值(倍数×20根中位)，amp 收盘偏离MA7(%)"""
    tot = dict(n=0, hit=0, nt=0, ht=0)
    per = {}
    for (code, period), d in data.items():
        df = d["df"]
        T = df["turnover"].to_numpy()
        med = d.setdefault("med20", pd.Series(T).rolling(20, min_periods=5).median().bfill().to_numpy())
        A = np.abs(df["close"] / df["ma7"] - 1).to_numpy() * 100
        idx, sides = [], []
        for i in d["up"]:
            if thr and T[i] < thr:
                continue
            if rel and T[i] < rel * med[i]:
                continue
            if amp and A[i] < amp:
                continue
            idx.append(i); sides.append(1)
        if gate_sell:
            for i in d["dn"]:
                if thr and T[i] < thr:
                    continue
                if rel and T[i] < rel * med[i]:
                    continue
                if amp and A[i] < amp:
                    continue
                idx.append(i); sides.append(-1)
        else:
            idx += list(d["dn"]); sides += [-1] * len(d["dn"])
        s = np.array([i for i in idx if d["lo"] <= i <= d["hi"]], dtype=int)
        t = d["ti"]
        hs = sum(1 for x in s if len(t) and np.min(np.abs(t - x)) <= tol)
        ht = sum(1 for x in t if len(s) and np.min(np.abs(s - x)) <= tol)
        per[f"{code}|{period}"] = dict(n=len(s), hit=hs, nt=len(t), ht=ht)
        tot["n"] += len(s); tot["hit"] += hs; tot["nt"] += len(t); tot["ht"] += ht
    p = tot["hit"] / tot["n"] if tot["n"] else 0
    r = tot["ht"] / tot["nt"] if tot["nt"] else 0
    return dict(prec=round(p, 4), rec=round(r, 4), n_sig=tot["n"],
                f1=round(2 * p * r / (p + r), 4) if p + r else 0), per


def main():
    truth = {tuple(k.split("|")): v for k, v in json.load(open(CACHE / "truth.json")).items()}
    data = build(truth)

    base, per = evaluate(data)
    print("基准（收盘穿MA7，无过滤）:", base)
    for k, v in per.items():
        print(f"   {k:20s} 信号{v['n']:3d} 命中{v['hit']:3d} 真值{v['nt']:3d} 精度{100*v['hit']/v['n'] if v['n'] else 0:3.0f}% 召回{100*v['ht']/v['nt']:3.0f}%")

    # ── 绝对换手阈值网格（按周期分开，量纲不同）──
    print("\n【第4步】绝对换手阈值网格")
    grids = {"day": np.round(np.arange(0, 5.01, 0.1), 2), "m60": np.round(np.arange(0, 1.21, 0.02), 3)}
    rows = []
    for period, grid in grids.items():
        sub = {k: v for k, v in data.items() if k[1] == period}
        for thr in grid:
            m, _ = evaluate(sub, thr=thr)
            rows.append(dict(period=period, thr=float(thr), **m))
    g = pd.DataFrame(rows)
    g.to_csv(ROOT / "output" / "calib_turnover_grid.csv", index=False)
    for period in grids:
        s = g[g.period == period]
        ok = s[s.n_sig >= 0.5 * s.iloc[0].n_sig]
        print(f"-- {period} 基准信号 {int(s.iloc[0].n_sig)}，保留>=50%信号时按精度 top5 --")
        print(ok.sort_values(["prec", "rec"], ascending=False).head(5).to_string(index=False))

    # ── 相对换手阈值（除以20根中位，跨周期可比）──
    print("\n【第4步·补】相对换手阈值（T / 20根中位）")
    rows = []
    for rel in np.round(np.arange(0, 2.01, 0.05), 2):
        m, _ = evaluate(data, rel=rel)
        rows.append(dict(rel=float(rel), **m))
    r = pd.DataFrame(rows)
    r.to_csv(ROOT / "output" / "calib_rel_grid.csv", index=False)
    ok = r[r.n_sig >= 0.5 * r.iloc[0].n_sig]
    print(ok.sort_values(["prec", "rec"], ascending=False).head(6).to_string(index=False))

    # ── 幅度阈值 ──
    print("\n【第5步·补】幅度阈值 |收盘/MA7-1| 下限")
    rows = []
    for amp in np.round(np.arange(0, 2.01, 0.05), 2):
        m, _ = evaluate(data, amp=amp)
        rows.append(dict(amp=float(amp), **m))
    a = pd.DataFrame(rows)
    a.to_csv(ROOT / "output" / "calib_amp_grid.csv", index=False)
    ok = a[a.n_sig >= 0.4 * a.iloc[0].n_sig]
    print(ok.sort_values(["prec", "rec"], ascending=False).head(6).to_string(index=False))

    # ── 二维：换手 × 幅度 ──
    print("\n【联合】换手(按周期) × 幅度 二维网格 top8")
    combos = []
    for thr_d in [0, 0.5, 1.0, 1.5, 2.0, 2.5]:
        for thr_m in [0, 0.05, 0.1, 0.15, 0.2, 0.3]:
            for amp in [0, 0.2, 0.3, 0.5, 0.8, 1.0]:
                tot = dict(n=0, hit=0, nt=0, ht=0)
                for period, thr in (("day", thr_d), ("m60", thr_m)):
                    sub = {k: v for k, v in data.items() if k[1] == period}
                    _, per = evaluate(sub, thr=thr, amp=amp)
                    for v in per.values():
                        for f in tot: tot[f] += v[f]
                p = tot["hit"] / tot["n"] if tot["n"] else 0
                rc = tot["ht"] / tot["nt"]
                combos.append(dict(thr_day=thr_d, thr_m60=thr_m, amp=amp, n_sig=tot["n"],
                                   prec=round(p, 3), rec=round(rc, 3),
                                   f1=round(2 * p * rc / (p + rc), 3) if p + rc else 0))
    c = pd.DataFrame(combos)
    c.to_csv(ROOT / "output" / "calib_joint_grid.csv", index=False)
    ok = c[c.n_sig >= 0.5 * c[c.amp == 0].n_sig.max()]
    print(ok.sort_values(["prec", "rec"], ascending=False).head(8).to_string(index=False))
    json.dump(dict(base=base, per=per), open(ROOT / "output" / "calib_final.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
