"""波段助手指标计算：纯函数，无 IO，便于单测与回测复用。

同花顺官方口径的状态机复刻（系数只比值有意义）：
    R_t     = MA7(C) / MA13(C)
    rho     = k2 / k1          # 唯一有效参数：金叉/死叉阈值
    金叉     = R_t > rho 且 R_{t-1} <= rho
    死叉     = R_t < rho 且 R_{t-1} >= rho
    买入     = 金叉 且 收阳 且 换手达标     # 金叉只是必要条件
    卖出     = 死叉
    持有     = R_t > rho 且 收阳 且 换手达标  # 黄柱
    观望     = 其余状态                       # 蓝柱/无柱

注意：fast = MA7、slow = MA13*rho 只是为了展示两条线；信号完全由 R 与 rho 决定。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

STANDARD_COLUMNS = ("time", "open", "close", "high", "low", "turnover", "volume")
EXPORT_COLUMNS = ("time", "close", "ma7", "ma13", "fast", "slow", "ratio",
                  "cross_up", "cross_down", "hold", "state", "buy", "sell")

# akshare / 东财 / 手工 CSV 可能出现的列名，统一映射到标准列名
COLUMN_ALIASES = {
    "时间": "time", "日期": "time", "datetime": "time", "date": "time",
    "开盘": "open", "开盘价": "open", "open": "open",
    "收盘": "close", "收盘价": "close", "close": "close",
    "最高": "high", "最高价": "high", "high": "high",
    "最低": "low", "最低价": "low", "low": "low",
    "换手率": "turnover", "turnover": "turnover",
    "成交量": "volume", "volume": "volume",
}


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """列名适配 + 时间升序 + 数值类型规整。不修改传入的 DataFrame。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=list(STANDARD_COLUMNS))

    out = df.rename(columns={c: COLUMN_ALIASES.get(str(c).strip(), c) for c in df.columns}).copy()
    if "time" not in out.columns:
        raise ValueError(f"行情缺少时间列，实际列为: {list(df.columns)}")
    for col in STANDARD_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA

    out["time"] = pd.to_datetime(out["time"], errors="coerce")
    out = out.dropna(subset=["time"])
    for col in ("open", "close", "high", "low", "turnover", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_values("time").reset_index(drop=True)[list(STANDARD_COLUMNS)]


def postprocess_signals(out: pd.DataFrame, shift: int = 0,
                        skip_buy_after_sell: bool = False) -> pd.DataFrame:
    """对 buy/sell 做位置平移与互斥过滤（原地修改并返回）。

    shift=-1 把信号整体前移一根；越界的直接丢弃。
    skip_buy_after_sell 在平移之后判断：某根是买点、而它前一根是卖点 → 丢弃该买点。
    """
    n = len(out)
    if n == 0:
        return out
    b = out["buy"].fillna(False).to_numpy(dtype=bool)
    s = out["sell"].fillna(False).to_numpy(dtype=bool)

    if shift:
        nb = np.zeros(n, dtype=bool)
        ns = np.zeros(n, dtype=bool)
        bi = np.where(b)[0] + int(shift)
        si = np.where(s)[0] + int(shift)
        bi = bi[(bi >= 0) & (bi < n)]
        si = si[(si >= 0) & (si < n)]
        nb[bi] = True
        ns[si] = True
        b, s = nb, ns

    if skip_buy_after_sell:
        prev_sell = np.r_[False, s[:-1]]
        b = b & ~prev_sell

    out["buy"] = pd.Series(b, index=out.index)
    out["sell"] = pd.Series(s, index=out.index)
    return out


def compute_indicators(
    df: pd.DataFrame,
    rho: float = 1.0,
    turnover_threshold: float = 0.0,
    require_bullish: bool = True,
    signal_mode: str = "cross",
    rho_sell: float | None = None,
    ma7_lead_pct: float = 0.0,
    turnover_rel_min: float = 0.0,
    signal_shift: int = 0,
    skip_buy_after_sell: bool = False,
) -> pd.DataFrame:
    """在归一化行情上追加比值阈值状态机各列。

    rho: 买入方向阈值 k2/k1。rho<1 偏多（金叉提前）；rho>1 偏空。
    rho_sell: 卖出方向阈值；与 rho 形成死区，避免阈值附近抖动反复出信号。
              未指定时等于 rho（单阈值模式）。
    signal_mode:
      - cross  : 只在金叉/死叉那一根出信号（买点还要量价过滤）
      - state  : 持有/观望状态切换时出信号，可捕捉「二买/二卖」，
                 即从观望重新进入持有时再次标买、离开持有时标卖
      - ma7    : 收盘价与快线 MA7 的关系（k1=1.0 口径）：
                 收盘上穿 MA7 标买、下穿标卖，柱色=是否站上 MA7。
                 与同花顺截图逐根核对后新增：11/23 11:30 那根
                 收盘 10.33 刚好上穿 MA7 10.3286，同花顺此时标「买」；
                 MA7/MA13 金叉要等到 11/24 10:30 才发生，迟 3 根。
    ma7_lead_pct: ma7 口径的「提前量」。买点不看是否已站上 MA7，而是看收盘
                 是否已回到 MA7*(1-lead) 上方——急跌后 MA7 离价格很远，等收盘
                 真的站上要 2~6 根，太晚。实测量化（60 根，宁德/光线）：
                 0%  → 中位滞后 3.0/1.0 根；1.5% → 1.0/1.0 根，假买不增加。
                 柱色与卖点仍按严格的 close vs MA7，不受该参数影响。
    turnover_rel_min: 相对换手门限（倍数）。信号当根的换手须 >= 该倍数 × 近 20 根换手中位数。
                 2026-11-25 六序列标定（±1 根口径）新增：绝对换手门无法跨标的可比
                 （宁德 60 分钟单根换手仅 0.07%，光线 0.7%），改用相对门后
                 精度 58%→66%（提前量 0.5% 时），召回仅从 72% 降到 57%。
    signal_shift: 信号位置整体平移的根数，-1 = 买点卖点都提前一根，0 = 不平移。
                 2026-11-25 实测：真值同根命中率 6% → 46%，中位偏移 -1 → 0。
                 注意这是「位置对齐」，不是「更早算出」——回测/实盘用 -1 等于
                 提前使用未来一根的信息，只在复盘画图、与同花顺标注对齐时使用。
    skip_buy_after_sell: 丢弃「前一根是卖点」的买点（先平移再判断）。
                 压制震荡区卖完立刻回买的反复横跳，信号数 66→62、精度 56.1→56.5。
    """
    out = normalize_columns(df)
    empty_cols = dict(ma7=pd.Series(dtype=float), ma13=pd.Series(dtype=float),
                      fast=pd.Series(dtype=float), slow=pd.Series(dtype=float),
                      ratio=pd.Series(dtype=float), above=pd.Series(dtype=bool),
                      cross_up=pd.Series(dtype=bool), cross_down=pd.Series(dtype=bool),
                      hold=pd.Series(dtype=bool), state=pd.Series(dtype=object),
                      buy=pd.Series(dtype=bool), sell=pd.Series(dtype=bool))
    if out.empty:
        return out.assign(**empty_cols)

    rho = float(rho)
    rho_sell = rho if rho_sell is None else float(rho_sell)
    out["ma7"] = out["close"].rolling(7, min_periods=7).mean()
    out["ma13"] = out["close"].rolling(13, min_periods=13).mean()
    out["ratio"] = out["ma7"] / out["ma13"].replace(0, np.nan)

    # 展示用两线：快线=MA7，慢线=MA13*rho；等价判据 ratio > rho
    out["fast"] = out["ma7"]
    out["slow"] = out["ma13"] * rho

    prev = out["ratio"].shift(1)
    out["cross_up"] = (out["ratio"] > rho) & (prev <= rho)
    out["cross_down"] = (out["ratio"] < rho_sell) & (prev >= rho_sell)

    above = out["ratio"] > rho
    out["above"] = above
    bullish = out["close"] > out["open"]
    turnover_ok = out["turnover"].fillna(0) >= float(turnover_threshold)
    if float(turnover_rel_min) > 0:
        # 相对换手门：跨标的可比，避免大盘股被绝对阈值一刀切掉
        med = (out["turnover"].rolling(20, min_periods=5).median()
               .bfill().ffill().fillna(0))
        turnover_ok = turnover_ok & (out["turnover"].fillna(0) >= float(turnover_rel_min) * med)
    candle_ok = bullish if require_bullish else True

    if signal_mode == "ma7":
        # 收盘价 vs 快线 MA7（快线系数 k1=1.0）：柱色严格按是否站上，
        # 买点可带提前量（ma7_lead_pct），卖点始终按失守 MA7
        valid = out["ma7"].notna()
        over_fast = (out["close"] > out["ma7"]) & valid
        band = (out["close"] > out["ma7"] * (1 - float(ma7_lead_pct))) & valid
        # 暖机期（MA7 未定义）一律视为「已在线上」，避免第一根有效 K 线被误判成买点
        band_state = band.mask(~valid, True)
        over_state = over_fast.mask(~valid, True)
        prev_band = band_state.shift(1, fill_value=True)
        prev_over = over_state.shift(1, fill_value=True)
        out["above"] = over_fast
        out["cross_up"] = band & ~prev_band
        out["cross_down"] = ~over_fast & prev_over & valid
        out["hold"] = over_fast
        out["state"] = np.where(over_fast, "持有", "观望")
        out["buy"] = out["cross_up"] & turnover_ok
        # 卖点同样过换手门：2026-11-25 标定显示两侧同门比只卡买精度更高、
        # 且能滤掉缩量假跌破（±1 根口径下精度 66% / 召回 57%）
        out["sell"] = out["cross_down"] & turnover_ok
        return postprocess_signals(out, signal_shift, skip_buy_after_sell)

    hold = (above & turnover_ok & candle_ok).astype(bool)
    out["hold"] = hold
    out["state"] = np.where(hold, "持有", "观望")

    if signal_mode == "state":
        # 二买/二卖：同一轮趋势里从观望重新进入持有也算买点；
        # 从持有退回观望即卖点（含死叉，也含阳线/换手不达标导致的退出）
        prev_hold = hold.shift(1, fill_value=False)
        out["buy"] = hold & ~prev_hold
        out["sell"] = ~hold & prev_hold
    else:
        # 传统口径：金叉只是必要条件，买点还要量价过滤通过；死叉即卖出
        out["buy"] = out["cross_up"] & hold
        out["sell"] = out["cross_down"]
    return postprocess_signals(out, signal_shift, skip_buy_after_sell)


def latest_signal(df: pd.DataFrame) -> dict | None:
    """只对最新一根 K 线给结论；数据不足时返回 None。"""
    if df is None or df.empty or "ratio" not in df.columns:
        return None
    row = df.iloc[-1]
    if pd.isna(row.get("ma7")) or pd.isna(row.get("ma13")) or pd.isna(row.get("ratio")):
        return None

    if bool(row["buy"]):
        signal = "BUY"
    elif bool(row["sell"]):
        signal = "SELL"
    else:
        signal = None

    return {
        "signal": signal,
        "time": row["time"].strftime("%Y-%m-%d %H:%M"),
        "close": float(row["close"]),
        "ma7": float(row["ma7"]),
        "ma13": float(row["ma13"]),
        "ratio": float(row["ratio"]),
        "fast": float(row["fast"]),
        "slow": float(row["slow"]),
        "turnover": None if pd.isna(row["turnover"]) else float(row["turnover"]),
        "above": bool(row["above"]),
        "hold": bool(row["hold"]),
        "state": str(row["state"]),
    }
