"""合成数据单测：指标交叉、持有规则、列名适配与 K 线闭合校验。"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indicator import compute_indicators, latest_signal, normalize_columns  # noqa: E402
from utils import validate_last_bar  # noqa: E402


def make_df() -> pd.DataFrame:
    """先 13 根 10 元、14 根 20 元、13 根 10 元：必有金叉与死叉各一次。"""
    closes = [10.0] * 13 + [20.0] * 14 + [10.0] * 13
    times = pd.date_range("2026-11-02 10:30", periods=len(closes), freq="60min")
    return pd.DataFrame({
        "time": times,
        "open": [c - 1 for c in closes],
        "close": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "turnover": [1.5] * len(closes),
        "volume": [1000] * len(closes),
    })


class IndicatorTests(unittest.TestCase):
    def test_normalize_columns_aliases_and_sort(self):
        df = make_df().rename(columns={
            "time": "日期", "open": "开盘价", "close": "收盘价",
            "turnover": "换手率", "volume": "成交量",
        }).iloc[::-1]
        out = normalize_columns(df)
        self.assertEqual(list(out.columns)[:3], ["time", "open", "close"])
        self.assertTrue(out["time"].is_monotonic_increasing)
        self.assertEqual(out.iloc[0]["close"], 10.0)

    def test_cross_up_and_cross_down_via_ratio(self):
        result = compute_indicators(make_df(), rho=1.0,
                                    turnover_threshold=1.0, require_bullish=True)
        ups = result.index[result["cross_up"]].tolist()
        downs = result.index[result["cross_down"]].tolist()
        self.assertEqual(ups, [13])
        self.assertEqual(downs, [27])
        self.assertAlmostEqual(result.loc[13, "ratio"],
                               result.loc[13, "ma7"] / result.loc[13, "ma13"])
        self.assertEqual(latest_signal(result.iloc[:28])["signal"], "SELL")
        self.assertIsNone(latest_signal(result)["signal"])  # 最后一根本身无交叉

    def test_buy_requires_cross_up_and_volume_filter(self):
        df = make_df()
        up_bar = compute_indicators(df.iloc[:14], rho=1.0, turnover_threshold=1.0)
        self.assertEqual(latest_signal(up_bar)["signal"], "BUY")
        self.assertEqual(latest_signal(up_bar)["state"], "持有")

        # 金叉成立但换手不足 -> 不推送买点，仅为观望
        weak = df.iloc[:14].copy()
        weak.loc[13, "turnover"] = 0.5
        weak_result = compute_indicators(weak, rho=1.0, turnover_threshold=1.0)
        self.assertTrue(bool(weak_result.iloc[-1]["cross_up"]))
        self.assertIsNone(latest_signal(weak_result)["signal"])
        self.assertEqual(latest_signal(weak_result)["state"], "观望")

        early = compute_indicators(df.iloc[:13], rho=1.0, turnover_threshold=1.0)
        self.assertIsNone(latest_signal(early)["signal"])

    def test_hold_requires_above_threshold_and_bullish_candle(self):
        df = make_df()
        df.loc[13, "turnover"] = 0.5  # 低于阈值 -> 观望
        df.loc[14, "open"] = df.loc[14, "close"] + 1  # 阴线 -> 观望
        result = compute_indicators(df, rho=1.0, turnover_threshold=1.0,
                                    require_bullish=True)
        self.assertFalse(bool(result.loc[13, "hold"]))
        self.assertFalse(bool(result.loc[14, "hold"]))
        self.assertTrue(bool(result.loc[15, "hold"]))
        self.assertFalse(bool(result.loc[27, "above"]))  # 死叉当日已跌破阈值
        # 死叉后 below 阈值，即使收阳换手足也不持有
        self.assertFalse(bool(result.loc[28, "above"]))
        self.assertFalse(bool(result.loc[28, "hold"]))

    def test_rho_changes_cross_timing(self):
        df = make_df()
        base = compute_indicators(df, rho=1.0, turnover_threshold=1.0)
        bearish = compute_indicators(df, rho=1.10, turnover_threshold=1.0)
        base_up = base.index[base["cross_up"]][0]
        base_down = base.index[base["cross_down"]][0]
        # rho>1 偏空：金叉滞后、死叉提前
        self.assertGreaterEqual(bearish.index[bearish["cross_up"]][0], base_up)
        self.assertLessEqual(bearish.index[bearish["cross_down"]][0], base_down)

    def test_separate_sell_threshold_moves_death_cross_earlier(self):
        df = make_df()
        base = compute_indicators(df, rho=1.0, rho_sell=1.0, turnover_threshold=1.0)
        wide = compute_indicators(df, rho=1.0, rho_sell=1.10, turnover_threshold=1.0)
        self.assertLessEqual(wide.index[wide["cross_down"]][0],
                             base.index[base["cross_down"]][0])
        # 买点阈值不受 rho_sell 影响
        self.assertEqual(wide.index[wide["cross_up"]].tolist(),
                         base.index[base["cross_up"]].tolist())

    def test_ma7_mode_buy_is_close_crossing_fast_line(self):
        """ma7 口径：收盘上穿 MA7 即买，比 MA7/MA13 金叉更早。"""
        df = make_df()
        out = compute_indicators(df, signal_mode="ma7", turnover_threshold=1.0)
        self.assertEqual(out.index[out["buy"]].tolist()[0], 13)
        self.assertTrue(bool(out.loc[13, "above"]))
        self.assertFalse(bool(out.loc[12, "above"]))
        # 第一根可计算 MA7 的 K 线不标买（无前值可比）
        self.assertFalse(bool(out.loc[6, "buy"]))
        self.assertIn("持有", out.loc[13, "state"])
        self.assertIn("观望", out.loc[27, "state"])

    def test_ma7_mode_turnover_gate_blocks_buy(self):
        df = make_df()
        df.loc[13, "turnover"] = 0.2
        out = compute_indicators(df, signal_mode="ma7", turnover_threshold=1.0)
        self.assertTrue(bool(out.loc[13, "cross_up"]))
        self.assertFalse(bool(out.loc[13, "buy"]))   # 换手不达标不出买点
        self.assertTrue(bool(out.loc[13, "hold"]))   # 但仍算站上快线

    def test_ma7_lead_pct_triggers_before_reclaiming_line(self):
        """提前量：收盘还在 MA7 下方 1% 内就标买；0 提前量要等真正站上。"""
        closes = [10.0] * 10 + [9.0] * 6 + [8.9]
        times = pd.date_range("2026-11-03 10:30", periods=len(closes), freq="60min")
        df = pd.DataFrame({
            "time": times,
            "open": [c + 0.2 for c in closes],
            "close": closes,
            "high": [c + 0.3 for c in closes],
            "low": [c - 0.3 for c in closes],
            "turnover": [2.0] * len(closes),
            "volume": [1000] * len(closes),
        })
        strict = compute_indicators(df, signal_mode="ma7", turnover_threshold=0.1)
        lead = compute_indicators(df, signal_mode="ma7", turnover_threshold=0.1,
                                  ma7_lead_pct=0.015)
        self.assertEqual(strict.index[strict["buy"]].tolist(), [])
        self.assertEqual(lead.index[lead["buy"]].tolist(), [16])
        self.assertFalse(bool(lead.loc[16, "above"]))   # 柱色仍严格按站上 MA7
        self.assertEqual(lead.loc[16, "state"], "观望")

    def test_turnover_rel_min_gates_both_sides(self):
        """相对换手门：换手低于自身近 20 根中位的 0.95 倍时不出信号（买/卖都卡）。"""
        closes = [10.0] * 10 + [9.0, 8.0, 7.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0]
        times = pd.date_range("2026-11-03 10:30", periods=len(closes), freq="60min")
        df = pd.DataFrame({
            "time": times,
            "open": [c + 0.1 for c in closes],
            "close": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "turnover": [1.0] * len(closes),
            "volume": [1000] * len(closes),
        })
        base = compute_indicators(df, signal_mode="ma7")
        n_base = int(base["buy"].sum() + base["sell"].sum())
        self.assertGreater(n_base, 0)
        # 只把信号那几根的换手压到中位以下，其余维持 1.0
        low = base.index[base["buy"] | base["sell"]].tolist()
        df2 = df.copy()
        df2.loc[low, "turnover"] = 0.2
        gated = compute_indicators(df2, signal_mode="ma7", turnover_rel_min=0.95)
        self.assertEqual(int(gated["buy"].sum() + gated["sell"].sum()), 0)

    def test_state_mode_captures_second_buy(self):
        df = make_df()
        # 第 13 根金叉并进入持有；第 14 根收阴退回观望；第 15 根再收阳 -> 二买
        df.loc[14, "open"] = df.loc[14, "close"] + 1
        out = compute_indicators(df, rho=1.0, turnover_threshold=1.0,
                                 signal_mode="state")
        buys = out.index[out["buy"]].tolist()
        sells = out.index[out["sell"]].tolist()
        self.assertIn(13, buys)
        self.assertIn(15, buys)   # 二买：重新进入持有，不依赖再次金叉
        self.assertIn(14, sells)  # 从持有退回观望即卖
        cross = compute_indicators(df, rho=1.0, turnover_threshold=1.0,
                                   signal_mode="cross")
        self.assertEqual(cross.index[cross["buy"]].tolist(), [13])
        self.assertEqual(cross.index[cross["sell"]].tolist(), [27])


class ClosureTests(unittest.TestCase):
    def test_m60_drops_unclosed_last_bar(self):
        df = pd.DataFrame({
            "time": ["2026-11-24 14:00", "2026-11-24 14:30"],
            "open": [10, 11], "close": [11, 12], "turnover": [1.0, 1.0], "volume": [100, 200],
        })
        ok, reason, clean = validate_last_bar(df, "m60", now=datetime(2026, 11, 24, 14, 35))
        self.assertTrue(ok)
        self.assertEqual(clean.iloc[-1]["time"].strftime("%H:%M"), "14:00")
        self.assertIn("不是标准收线时点", reason)

    def test_m60_rejects_premature_close_bar(self):
        df = pd.DataFrame({
            "time": ["2026-11-24 14:00", "2026-11-24 15:00"],
            "open": [10, 11], "close": [11, 12], "turnover": [1.0, 1.0], "volume": [100, 200],
        })
        ok, _, clean = validate_last_bar(df, "m60", now=datetime(2026, 11, 24, 14, 35))
        self.assertTrue(ok)
        self.assertEqual(clean.iloc[-1]["time"].strftime("%H:%M"), "14:00")

    def test_daily_drops_today_before_close(self):
        df = pd.DataFrame({
            "time": ["2026-11-23", "2026-11-24"],
            "open": [10, 11], "close": [11, 12], "turnover": [2.0, 2.0], "volume": [100, 200],
        })
        ok, _, clean = validate_last_bar(df, "daily", now=datetime(2026, 11, 24, 12, 0))
        self.assertTrue(ok)
        self.assertEqual(clean.iloc[-1]["time"].strftime("%Y-%m-%d"), "2026-11-23")

    def test_signal_shift_moves_signals_one_bar_earlier(self):
        """signal_shift=-1 应把买点卖点各前移一根，且不改变信号数量。"""
        df = make_df()
        base = compute_indicators(df, signal_mode="ma7")
        shifted = compute_indicators(df, signal_mode="ma7", signal_shift=-1)
        b0 = [i for i, v in enumerate(base["buy"]) if v]
        b1 = [i for i, v in enumerate(shifted["buy"]) if v]
        s0 = [i for i, v in enumerate(base["sell"]) if v]
        s1 = [i for i, v in enumerate(shifted["sell"]) if v]
        self.assertGreater(len(b0), 0)
        # 首根信号平移到 -1 会被丢弃，其余整体 -1
        self.assertEqual(b1, [i - 1 for i in b0 if i - 1 >= 0])
        self.assertEqual(s1, [i - 1 for i in s0 if i - 1 >= 0])

    def test_signal_shift_drops_out_of_range(self):
        """第 0 根的信号前移一根后越界，必须丢弃而不是回卷到末根。"""
        from indicator import postprocess_signals
        out = pd.DataFrame({"buy": [True, False, False],
                            "sell": [False, False, True]})
        res = postprocess_signals(out.copy(), shift=-1)
        self.assertEqual(list(res["buy"]), [False, False, False])
        self.assertEqual(list(res["sell"]), [False, True, False])

    def test_skip_buy_after_sell_removes_rebuy(self):
        """前一根是卖点的买点要被丢弃；前一根不是卖点的买点保留。"""
        import numpy as np
        from indicator import postprocess_signals
        out = pd.DataFrame({"buy": [False, False, True, False, True],
                            "sell": [False, True, False, False, False]})
        res = postprocess_signals(out.copy(), shift=0, skip_buy_after_sell=True)
        # 索引 2 的买点前一根（索引 1）是卖点 → 丢弃；索引 4 保留
        self.assertEqual(list(res["buy"]), [False, False, False, False, True])
        # 不平移时不影响卖点
        self.assertEqual(list(res["sell"]), [False, True, False, False, False])

    def test_skip_buy_after_sell_runs_after_shift(self):
        """判断用的是平移后的位置：原始 buy[i] 与 sell[i-1]，平移后 buy 在 i-1、
        sell 在 i-2，仍相邻，应被丢弃。"""
        from indicator import postprocess_signals
        out = pd.DataFrame({"buy": [False, False, False, True],
                            "sell": [False, False, True, False]})
        res = postprocess_signals(out.copy(), shift=-1, skip_buy_after_sell=True)
        self.assertEqual(list(res["buy"]), [False, False, False, False])
        self.assertEqual(list(res["sell"]), [False, True, False, False])

    def test_sell_without_shift_keeps_buy(self):
        """只有卖点、没有卖点压制时，买点不该被削掉。"""
        from indicator import postprocess_signals
        out = pd.DataFrame({"buy": [False, True, False], "sell": [False, False, True]})
        res = postprocess_signals(out.copy(), shift=0, skip_buy_after_sell=True)
        self.assertEqual(list(res["buy"]), [False, True, False])


if __name__ == "__main__":
    unittest.main()
