"""黑箱标定：把同花顺「波段助手」当对象，网格搜索参数复现其买卖标记。

流程（对应标定五步法）：
    1. 前复权对齐（日线走 qfq；60 分钟线数据源强制不复权，脚本会告警）
    2. 读入真值序列 truth.csv（time,side），side ∈ {B,S}
    3. rho 网格 0.95~1.05 步长 0.001，比对穿越点与真值的匹配率
    4. 固定最优 rho，再对换手阈值网格搜索
    5. 多标的汇总，看是否存在跨股稳定的参数

匹配口径（可调容忍窗口 tol）：
    recall    = 被至少一个信号命中的真值点数 / 真值点总数
    precision = 被真值认领的信号数 / 信号总数
    f1        = 2PR/(P+R)

用法：
    python tools/calibrate.py --code 300251 --period m60 \
        --truth data/truth/300251_m60.csv --mode cross \
        --rho-min 0.95 --rho-max 1.05 --rho-step 0.001 --tol 0 1 2 3

    # 一次性比较多只票，找「跨股稳定」的参数
    python tools/calibrate.py --batch --truth-dir data/truth

真值 CSV 格式（首行表头，时间与行情同一时区/周期）：
    time,side
    2026-11-04 10:30,B
    2026-11-04 15:00,S
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from indicator import compute_indicators  # noqa: E402


# ---------------------------------------------------------------- 真值与匹配

def load_truth(path: Path) -> pd.DataFrame:
    """读真值：time + side(B/S)。容忍中文列名与「买/卖」。"""
    df = pd.read_csv(path)
    df.columns = [str(c).strip().lower() for c in df.columns]
    tcol = next((c for c in ("time", "date", "datetime", "时间", "日期") if c in df.columns), None)
    scol = next((c for c in ("side", "signal", "type", "方向", "信号") if c in df.columns), None)
    if tcol is None or scol is None:
        raise ValueError(f"{path.name} 需要 time 与 side 两列，实际: {list(df.columns)}")
    df = df.rename(columns={tcol: "time", scol: "side"})
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df["side"] = (df["side"].astype(str).str.strip().str.upper()
                  .replace({"买": "B", "买入": "B", "BUY": "B",
                            "卖": "S", "卖出": "S", "SELL": "S"}))
    df = df.dropna(subset=["time"])
    df = df[df["side"].isin(["B", "S"])]
    return df.sort_values("time").reset_index(drop=True)


def match_signal(sig_idx: np.ndarray, truth_idx: np.ndarray, tol: int) -> dict:
    """在索引轴上做双向匹配。sig_idx / truth_idx 均已按时间对齐到同一根。"""
    hit_truth, used = 0, set()
    for t in truth_idx:
        cand = [s for s in sig_idx if abs(int(s) - int(t)) <= tol and s not in used]
        if cand:
            # 取距离最近的那根，避免同一信号被两条真值重复认领
            best = min(cand, key=lambda s: abs(int(s) - int(t)))
            used.add(best)
            hit_truth += 1
    n_sig, n_truth = len(sig_idx), len(truth_idx)
    recall = hit_truth / n_truth if n_truth else 0.0
    precision = len(used) / n_sig if n_sig else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"signal": n_sig, "truth": n_truth, "hit": hit_truth,
            "precision": precision, "recall": recall, "f1": f1}


def align(quotes: pd.DataFrame, truth: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """把真值时间映射成行情行号；行情里没有的时间丢弃（并回报）。"""
    pos = {t: i for i, t in enumerate(quotes["time"])}
    b, s, missing = [], [], []
    for _, row in truth.iterrows():
        i = pos.get(row["time"])
        if i is None:
            missing.append(row["time"])
            continue
        (b if row["side"] == "B" else s).append(i)
    if missing:
        print(f"  ! {len(missing)} 条真值时间不在行情里（前 3 条: "
              f"{[str(t) for t in missing[:3]]}），已跳过")
    return np.array(sorted(b), dtype=int), np.array(sorted(s), dtype=int)


# ---------------------------------------------------------------- 参数网格

@dataclass
class GridResult:
    params: dict
    buy: dict
    sell: dict
    n_buy: int
    n_sell: int

    @property
    def score(self) -> float:
        """买卖加权：真值里买卖数量通常不等，用 f1 直接平均会偏，按真值数加权。"""
        wb, ws = self.buy["truth"], self.sell["truth"]
        tot = wb + ws
        if tot == 0:
            return 0.0
        return (self.buy["f1"] * wb + self.sell["f1"] * ws) / tot


def eval_params(quotes: pd.DataFrame, tb: np.ndarray, ts: np.ndarray,
                tol: int, **kw) -> GridResult:
    ind = compute_indicators(quotes, **kw)
    sig_b = np.where(ind["buy"].fillna(False).values)[0]
    sig_s = np.where(ind["sell"].fillna(False).values)[0]
    return GridResult(params=kw,
                      buy=match_signal(sig_b, tb, tol),
                      sell=match_signal(sig_s, ts, tol),
                      n_buy=len(sig_b), n_sell=len(sig_s))


def sweep_rho(quotes, tb, ts, tol, mode, rng, base) -> pd.DataFrame:
    rows = []
    for rho in rng:
        r = eval_params(quotes, tb, ts, tol, signal_mode=mode, rho=rho, **base)
        rows.append({"rho": round(float(rho), 4),
                     "买": r.n_buy, "买命中": r.buy["hit"],
                     "买P": r.buy["precision"], "买R": r.buy["recall"], "买F1": r.buy["f1"],
                     "卖": r.n_sell, "卖命中": r.sell["hit"],
                     "卖P": r.sell["precision"], "卖R": r.sell["recall"], "卖F1": r.sell["f1"],
                     "综合": r.score})
    return pd.DataFrame(rows)


def sweep_threshold(quotes, tb, ts, tol, mode, rho, rng, base) -> pd.DataFrame:
    rows = []
    for thr in rng:
        r = eval_params(quotes, tb, ts, tol, signal_mode=mode, rho=rho,
                        turnover_threshold=float(thr), **base)
        rows.append({"换手%": round(float(thr), 3), "买": r.n_buy, "买命中": r.buy["hit"],
                     "买P": r.buy["precision"], "买R": r.buy["recall"],
                     "卖": r.n_sell, "卖命中": r.sell["hit"],
                     "综合": r.score})
    return pd.DataFrame(rows)


def sweep_lead(quotes, tb, ts, tol, base) -> pd.DataFrame:
    rows = []
    for lead in np.arange(0, 0.0501, 0.005):
        r = eval_params(quotes, tb, ts, tol, signal_mode="ma7",
                        ma7_lead_pct=round(float(lead), 4), **base)
        rows.append({"提前量%": round(float(lead) * 100, 2), "买": r.n_buy,
                     "买命中": r.buy["hit"], "买P": r.buy["precision"],
                     "买R": r.buy["recall"], "买F1": r.buy["f1"],
                     "卖": r.n_sell, "卖命中": r.sell["hit"], "综合": r.score})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 数据加载

def load_quotes(code: str, period: str, csv: Path | None) -> pd.DataFrame:
    for cand in ([csv] if csv else []) + [ROOT / "data" / "cache" / f"{code}_{period}.csv",
                                          ROOT / "data" / "cache" / f"{code}_{period}_b.csv"]:
        if cand and Path(cand).exists():
            df = pd.read_csv(cand, parse_dates=["time"])
            print(f"  行情: {Path(cand).name}  {len(df)} 根  "
                  f"{df['time'].iloc[0]:%Y-%m-%d %H:%M} → {df['time'].iloc[-1]:%Y-%m-%d %H:%M}")
            return df
    raise FileNotFoundError(f"找不到 {code}_{period}.csv，请先缓存或给 --quotes")


def warn_adjust(period: str) -> None:
    if period == "m60":
        print("  ! 60 分钟线数据源强制不复权，除权日前后 MA 会被跳空污染"
              "（标定五步法的第 1 步无法完全满足）")


def main() -> int:
    ap = argparse.ArgumentParser(description="波段助手参数标定")
    ap.add_argument("--code", default="300251")
    ap.add_argument("--period", default="m60")
    ap.add_argument("--quotes", type=Path, help="行情 CSV（默认 data/cache/<code>_<period>.csv）")
    ap.add_argument("--truth", type=Path, help="真值 CSV（time,side）")
    ap.add_argument("--mode", default="cross", choices=["cross", "state", "ma7"])
    ap.add_argument("--rho-min", type=float, default=0.95)
    ap.add_argument("--rho-max", type=float, default=1.05)
    ap.add_argument("--rho-step", type=float, default=0.001)
    ap.add_argument("--tol", type=int, nargs="+", default=[0, 1, 2, 3])
    ap.add_argument("--thr", type=float, default=0.0, help="固定换手阈值(%%)")
    ap.add_argument("--no-bullish", action="store_true", help="关闭收阳过滤")
    ap.add_argument("--json-out", type=Path, help="把网格结果写 JSON")
    args = ap.parse_args()

    if not args.truth:
        ap.error("需要 --truth 真值文件（time,side）")
    quotes = load_quotes(args.code, args.period, args.quotes)
    warn_adjust(args.period)
    truth = load_truth(args.truth)
    tb, ts = align(quotes, truth)
    print(f"  真值: {len(truth)} 条 → 对齐后 买 {len(tb)} / 卖 {len(ts)}")
    if len(tb) == 0 and len(ts) == 0:
        print("  ! 真值与行情时间完全不重叠，无法标定")
        return 1

    base = {"require_bullish": not args.no_bullish}
    fixed = {**base, "turnover_threshold": args.thr}
    rng = np.arange(args.rho_min, args.rho_max + 1e-9, args.rho_step)

    payload = {"code": args.code, "period": args.period, "mode": args.mode,
               "bars": len(quotes), "truth": {"buy": len(tb), "sell": len(ts)},
               "grids": {}}

    for tol in args.tol:
        print(f"\n=== 容忍窗口 ±{tol} 根 · {args.mode} 口径 · rho 网格 "
              f"{args.rho_min}~{args.rho_max} 步长 {args.rho_step}")
        table = sweep_rho(quotes, tb, ts, tol, args.mode, rng, fixed)
        best = table.loc[table["综合"].idxmax()]
        print(f"  最优 rho={best['rho']}  综合 {best['综合']:.3f}  "
              f"买 P/R {best['买P']:.2f}/{best['买R']:.2f}（{int(best['买'])} 个）  "
              f"卖 P/R {best['卖P']:.2f}/{best['卖R']:.2f}（{int(best['卖'])} 个）")
        # 打印几个代表性档位，避免只看一个数
        for q in (0.25, 0.5, 0.75):
            rho_q = float(rng[int(q * (len(rng) - 1))])
            row = table.loc[(table["rho"] - rho_q).abs().idxmin()]
            print(f"    rho={row['rho']:.3f}  买{int(row['买'])}个 命中{int(row['买命中'])} "
                  f"P{row['买P']:.2f} R{row['买R']:.2f} ｜ 卖{int(row['卖'])}个 "
                  f"命中{int(row['卖命中'])} P{row['卖P']:.2f} R{row['卖R']:.2f} 综合{row['综合']:.3f}")
        payload["grids"][f"rho_tol{tol}"] = table.to_dict("records")
        payload.setdefault("best", {})[f"tol{tol}"] = {
            "rho": float(best["rho"]), "score": float(best["综合"]),
            "buy": int(best["买"]), "sell": int(best["卖"])}

        if tol == args.tol[0]:
            rho_best = float(best["rho"])
            thr_table = sweep_threshold(quotes, tb, ts, tol, args.mode, rho_best,
                                        np.arange(0, 1.01, 0.05), base)
            print(f"  -- 固定 rho={rho_best} 扫换手阈值：")
            for _, r in thr_table.iterrows():
                print(f"     换手≥{r['换手%']:.2f}%  买{int(r['买'])}命中{int(r['买命中'])} "
                      f"R{r['买R']:.2f} ｜ 卖{int(r['卖'])}命中{int(r['卖命中'])} 综合{r['综合']:.3f}")
            payload["grids"][f"thr_tol{tol}"] = thr_table.to_dict("records")

            lead_table = sweep_lead(quotes, tb, ts, tol, fixed)
            print(f"  -- ma7 口径扫提前量：")
            for _, r in lead_table.iterrows():
                print(f"     提前{float(r['提前量%']):.1f}%  买{int(r['买'])}命中{int(r['买命中'])} "
                      f"P{r['买P']:.2f} R{r['买R']:.2f} ｜ 卖{int(r['卖'])}命中{int(r['卖命中'])}")
            payload["grids"][f"lead_tol{tol}"] = lead_table.to_dict("records")

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"\n网格结果已写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
