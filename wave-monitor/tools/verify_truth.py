#!/usr/bin/env python3
"""用生产代码 + 当前 config 回放 6 组真值，按 ±1 根口径打分。

用法: .venv/bin/python tools/verify_truth.py [--dump]
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


def idx_of(df, ts, period):
    df = df.assign(time=df["time"].astype(str))
    if period == "day":
        return int(df.index[df["time"].str[:10] == ts][0])
    d, t = ts.split(" ")
    hh, mm = map(int, t.split(":"))
    m = hh * 60 + mm
    best = min([630, 690, 840, 900], key=lambda c: abs(c - m))
    lab = f"{best // 60:02d}:{best % 60:02d}:00"
    return int(df.index[(df["time"].str[:10] == d) & (df["time"].str[11:] == lab)][0])


def main():
    cfg = yaml.safe_load(open(ROOT / "config.yaml"))
    truth = {tuple(k.split("|")): v for k, v in json.load(open(CACHE / "truth.json")).items()}
    rows, tot = [], dict(n=0, hit=0, nt=0, ht=0)
    dump = "--dump" in sys.argv
    for (code, period), tv in truth.items():
        df = pd.read_csv(CACHE / f"{code}_{period}.csv")
        if period == "m60":
            # 60 分钟原始数据不带换手率，按流通股本折算（与标定脚本同口径）
            fs = {"sz300251": 27.53e8, "sz300750": 42.61e8, "sz302132": 5.86e8}[code]
            df["turnover"] = df["volume"] * 100.0 / fs * 100.0
        cfg_period = "daily" if period == "day" else period
        res = calculate(df.copy(), cfg, cfg_period)
        ti = np.array([idx_of(res, t, period) for t, s in tv])
        ts = np.array([s for t, s in tv])
        lo, hi = ti.min() - 1, ti.max() + 1
        buy = np.where(res["buy"].to_numpy())[0]
        sell = np.where(res["sell"].to_numpy())[0]
        sig = [(i, 1) for i in buy if lo <= i <= hi] + [(i, -1) for i in sell if lo <= i <= hi]
        s_all = np.array([i for i, _ in sig], dtype=int)
        hit_s = 0
        for i, sd in sig:
            same = ti[ts == sd]
            if len(same) and np.min(np.abs(same - i)) <= 1:
                hit_s += 1
        hit_t = sum(1 for i, sd in zip(ti, ts)
                    if len(s_all) and np.min(np.abs(s_all - i)) <= 1)
        p = hit_s / len(sig) if sig else 0
        r = hit_t / len(ti)
        rows.append(dict(seq=f"{code}|{period}", n_sig=len(sig), hit=hit_s,
                         n_truth=len(ti), hit_truth=hit_t,
                         prec=round(100 * p), rec=round(100 * r)))
        tot["n"] += len(sig); tot["hit"] += hit_s; tot["nt"] += len(ti); tot["ht"] += hit_t
        if dump:
            print(f"\n--- {code} {period} ---")
            print("真值:", [(t, s) for t, s in tv])
            print("信号:", [(res['time'].iloc[i], '买' if sd == 1 else '卖') for i, sd in sig])
    t = pd.DataFrame(rows)
    print(t.to_string(index=False))
    print(f"\n合计  信号 {tot['n']}  命中 {tot['hit']}  精度 {100*tot['hit']/tot['n']:.1f}%"
          f"  |  真值 {tot['nt']}  覆盖 {tot['ht']}  召回 {100*tot['ht']/tot['nt']:.1f}%")
    t.to_csv(ROOT / "output" / "verify_truth.csv", index=False)


if __name__ == "__main__":
    main()
