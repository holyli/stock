#!/usr/bin/env python3
"""按 5 步黑箱标定法标定 R=MA7/MA13 的 rho 与换手阈值。

口径（用户 2026-11-25 指定）：
  - 精度优先：不追求召回全部真值点
  - 命中判定：信号与真值偏差 <= 1 根 K 线才算匹配
"""
import json
import numpy as np
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "calib"
FLOAT_SHARES = {          # 流通股本（股），由 流通市值/股价 反推
    "sz300251": 27.53e8,
    "sz300750": 42.61e8,
    "sz302132": 5.86e8,
}
SLOTS = ["10:30", "11:30", "14:00", "15:00"]


def load(code, period, truth):
    df = pd.read_csv(CACHE / f"{code}_{period}.csv")
    df["ma7"] = df["close"].rolling(7).mean()
    df["ma13"] = df["close"].rolling(13).mean()
    df["R"] = df["ma7"] / df["ma13"]
    if "turnover" not in df.columns or df["turnover"].isna().all():
        df["turnover"] = np.nan
    if period == "m60":
        df["turnover"] = df["volume"] * 100.0 / FLOAT_SHARES[code] * 100.0
    # 真值 -> bar 序号
    ti, ts_side = [], []
    for t, side in truth:
        if period == "day":
            hit = df.index[df["time"].str[:10] == t]
        else:
            d, hm = t.split(" ")
            hh, mm = map(int, hm.split(":"))
            m = hh * 60 + mm
            best = min([630, 690, 840, 900], key=lambda c: abs(c - m))
            lab = f"{best // 60:02d}:{best % 60:02d}:00"
            hit = df.index[(df["time"].str[:10] == d) & (df["time"].str[11:] == lab)]
        if len(hit):
            ti.append(int(hit[0]))
            ts_side.append(side)
    return df, np.array(ti), np.array(ts_side)


def score(sig_idx, truth_idx, lo, hi, tol=1):
    """返回 precision / recall / f1（只在真值覆盖区间内评估）"""
    s = np.array([i for i in sig_idx if lo <= i <= hi], dtype=int)
    t = np.array(truth_idx, dtype=int)
    if len(t) == 0:
        return None
    hit_t = sum(1 for x in t if len(s) and np.min(np.abs(s - x)) <= tol)
    hit_s = sum(1 for x in s if len(t) and np.min(np.abs(t - x)) <= tol)
    prec = hit_s / len(s) if len(s) else 0.0
    rec = hit_t / len(t)
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return dict(n_sig=len(s), prec=round(prec, 3), rec=round(rec, 3),
                f1=round(f1, 3), hit_sig=hit_s, hit_truth=hit_t, n_truth=len(t))


def cross_signals(R, rho):
    prev = R.shift(1)
    up = ((prev <= rho) & (R > rho)).to_numpy()
    dn = ((prev >= rho) & (R < rho)).to_numpy()
    return np.where(up)[0], np.where(dn)[0]


def band_signals(R, rb, rs):
    """滞回带：R 上穿 rb 买、下穿 rs 卖（rs < rb）"""
    st = np.zeros(len(R), dtype=int)
    cur = 0
    for i in range(len(R)):
        r = R[i]
        if np.isnan(r):
            st[i] = 0
            continue
        if cur == 0 and r > rb:
            cur = 1
        elif cur == 1 and r < rs:
            cur = 0
        st[i] = cur
    prev = np.r_[0, st[:-1]]
    up = (st == 1) & (prev == 0)
    dn = (st == 0) & (prev == 1)
    return np.where(up)[0], np.where(dn)[0]


def pool(results):
    """把多序列结果并成总体精度"""
    ns = sum(r["n_sig"] for r in results)
    hs = sum(r["hit_sig"] for r in results)
    ht = sum(r["hit_truth"] for r in results)
    nt = sum(r["n_truth"] for r in results)
    p = hs / ns if ns else 0.0
    rc = ht / nt if nt else 0.0
    return dict(n_sig=ns, prec=round(p, 3), rec=round(rc, 3),
                f1=round(2 * p * rc / (p + rc), 3) if p + rc else 0.0,
                n_truth=nt)


def main():
    truth = {tuple(k.split("|")): v for k, v in json.load(open(CACHE / "truth.json")).items()}
    cache = {}
    for key, tv in truth.items():
        code, period = key
        df, ti, tsd = load(code, period, tv)
        lo, hi = ti.min() - 1, ti.max() + 1
        cache[key] = (df, ti, tsd, lo, hi)
        print(f"{code} {period}: bars={len(df)} truth={len(ti)} window={lo}~{hi}")

    # ── 第 2 步：单 rho 网格 ──
    grid = np.round(np.arange(0.950, 1.0501, 0.001), 3)
    rows = []
    for rho in grid:
        res = []
        for key, (df, ti, tsd, lo, hi) in cache.items():
            up, dn = cross_signals(df["R"], rho)
            res.append(score(up, ti[tsd == 1], lo, hi))
            res.append(score(dn, ti[tsd == -1], lo, hi))
        res = [r for r in res if r]
        rows.append(dict(rho=float(rho), **pool(res)))
    g = pd.DataFrame(rows)
    g.to_csv(ROOT / "output" / "calib_rho_grid.csv", index=False)
    print("\n== 单 rho 网格：按精度排序 top10 ==")
    print(g.sort_values(["prec", "rec"], ascending=False).head(10).to_string(index=False))
    print("\n== 单 rho 网格：按 F1 排序 top5 ==")
    print(g.sort_values("f1", ascending=False).head(5).to_string(index=False))
    print("\n== rho=1.000 ==  ", g[g.rho == 1.0].to_dict("records"))

    # ── 非对称滞回带，二维网格 ──
    lo_, hi_ = 0.96, 1.04
    band = []
    step = 0.005
    for rb in np.round(np.arange(0.99, hi_ + 1e-9, step), 3):
        for rs in np.round(np.arange(lo_, rb - 1e-9, step), 3):
            res = []
            for key, (df, ti, tsd, l, h) in cache.items():
                up, dn = band_signals(df["R"].to_numpy(), rb, rs)
                res.append(score(up, ti[tsd == 1], l, h))
                res.append(score(dn, ti[tsd == -1], l, h))
            res = [r for r in res if r]
            band.append(dict(rho_buy=float(rb), rho_sell=float(rs), **pool(res)))
    b = pd.DataFrame(band)
    b.to_csv(ROOT / "output" / "calib_band_grid.csv", index=False)
    print("\n== 滞回带 (rb>rs) 按精度 top10 ==")
    print(b.sort_values(["prec", "rec"], ascending=False).head(10).to_string(index=False))
    print("\n== 滞回带 按 F1 top5 ==")
    print(b.sort_values("f1", ascending=False).head(5).to_string(index=False))

    json.dump(dict(rho_grid=g.to_dict("records"), band_top=b.sort_values(["prec", "rec"], ascending=False).head(20).to_dict("records")),
              open(ROOT / "output" / "calib_rho.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
