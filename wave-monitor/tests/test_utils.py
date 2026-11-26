"""交易日判断单测：A 股 / 港股双市场 OR 语义（不依赖网络，用打桩替换取数函数）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import utils  # noqa: E402


class _Stub:
    """临时替换 utils 里的两个市场判断函数。"""

    def __init__(self, cn: bool, hk: bool | None):
        self.cn, self.hk = cn, hk
        self._orig = (utils.is_trading_day, utils.is_hk_trading_day)

    def __enter__(self):
        utils.is_trading_day = lambda now=None: self.cn
        utils.is_hk_trading_day = lambda now=None: self.hk
        return self

    def __exit__(self, *exc):
        utils.is_trading_day, utils.is_hk_trading_day = self._orig


class TradingDayTests(unittest.TestCase):
    def test_scan_when_only_cn_open(self):
        """A 股开市、港股休市（如 7/1 香港回归纪念日）→ 仍要跑。"""
        with _Stub(cn=True, hk=False):
            self.assertTrue(utils.is_any_trading_day())
            self.assertTrue(utils.trading_day_status()["any"])

    def test_scan_when_only_hk_open(self):
        """港股开市、A 股休市（如国庆期间的 10/02）→ 仍要跑。"""
        with _Stub(cn=False, hk=True):
            self.assertTrue(utils.is_any_trading_day())
            self.assertTrue(utils.trading_day_status()["any"])

    def test_skip_when_both_closed(self):
        """两个市场都休市（如 10/01、周末）→ 不跑。"""
        with _Stub(cn=False, hk=False):
            self.assertFalse(utils.is_any_trading_day())
            self.assertFalse(utils.trading_day_status()["any"])

    def test_hk_unknown_falls_back_to_cn(self):
        """港股日历取不到（None）时，退化为只看 A 股，不阻断扫描。"""
        with _Stub(cn=True, hk=None):
            self.assertTrue(utils.is_any_trading_day())
        with _Stub(cn=False, hk=None):
            self.assertFalse(utils.is_any_trading_day())

    def test_is_today_bar_filters_stale(self):
        """非当日 K 线一律判 False，用于拦掉数据滞后产生的过期提醒。"""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026, 11, 25, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertTrue(utils.is_today_bar("2026-11-25 15:00", now))
        self.assertTrue(utils.is_today_bar("2026-11-25 00:00", now))   # 日线
        self.assertTrue(utils.is_today_bar("2026-11-25", now))
        self.assertFalse(utils.is_today_bar("2026-11-24 15:00", now))
        self.assertFalse(utils.is_today_bar("", now))

    def test_turnover_multiple(self):
        """量能倍数 = 当根换手 ÷ 前 20 根中位（不含当根）。"""
        import pandas as pd
        df = pd.DataFrame({"turnover": [1.0] * 20 + [3.0]})
        self.assertAlmostEqual(utils.turnover_multiple(df), 3.0, places=2)
        # 当根不参与中位计算：把当根算进去会得到不同结果
        df2 = pd.DataFrame({"turnover": [1.0] * 20 + [5.0]})
        self.assertAlmostEqual(utils.turnover_multiple(df2), 5.0, places=2)
        # 数据不足 / 全零时返回 0，不抛异常
        self.assertEqual(utils.turnover_multiple(pd.DataFrame({"turnover": [1.0]})), 0.0)
        self.assertEqual(utils.turnover_multiple(pd.DataFrame({"turnover": [0.0] * 25})), 0.0)

    def test_volume_tier(self):
        """量能档位分界：≥2.5 放量、≥1.5 温和放量、≥1.0 平量、否则缩量。"""
        self.assertEqual(utils.volume_tier(3.2)[0], "放量")
        self.assertEqual(utils.volume_tier(2.5)[0], "放量")
        self.assertEqual(utils.volume_tier(2.4)[0], "温和放量")
        self.assertEqual(utils.volume_tier(1.5)[0], "温和放量")
        self.assertEqual(utils.volume_tier(1.0)[0], "平量")
        self.assertEqual(utils.volume_tier(0.6)[0], "缩量")
        self.assertEqual(utils.volume_tier(0)[0], "")

    def test_status_fields(self):
        """状态字典要带 date/cn/hk/any 四项，供日志输出。"""
        with _Stub(cn=True, hk=True):
            st = utils.trading_day_status()
        self.assertEqual(set(st), {"date", "cn", "hk", "any"})
        self.assertRegex(st["date"], r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main()
