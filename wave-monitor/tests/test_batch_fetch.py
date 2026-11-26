"""批量取数与缓存新鲜度单测（不联网）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kline_cache  # noqa: E402
import utils  # noqa: E402
from fetcher import MarketFetcher  # noqa: E402


def _open_stub(value: bool = True):
    return mock.patch.object(utils, "market_open_on", lambda day, market="cn": value)


class ExpectedLastBarTests(unittest.TestCase):
    def test_m60_after_bar_close(self):
        """11:46 时 A 股本应已收线的是 11:30 那根。"""
        now = datetime(2026, 11, 26, 11, 46)
        with _open_stub(True):
            self.assertEqual(utils.expected_last_bar("m60", "cn", now),
                             datetime(2026, 11, 26, 11, 30))

    def test_m60_right_after_1430_bar(self):
        """14:03 本应已收线的是 14:00 那根（不是 11:30）。"""
        now = datetime(2026, 11, 26, 14, 3)
        with _open_stub(True):
            self.assertEqual(utils.expected_last_bar("m60", "cn", now),
                             datetime(2026, 11, 26, 14, 0))

    def test_m60_before_first_bar_uses_previous_session(self):
        """9:35 今天还没有收线，应回落到上一交易日的 15:00。"""
        now = datetime(2026, 11, 26, 9, 35)
        with _open_stub(False):
            expected = utils.expected_last_bar("m60", "cn", now)
        self.assertLess(expected, datetime(2026, 11, 26))
        self.assertEqual(expected.strftime("%H:%M"), "15:00")


class ParseBatchTableTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = MarketFetcher({"fetch": {}})
        self.now = utils.sh_now().replace(tzinfo=None)

    def _table(self, rows):
        head = ("| code | date | open | last | high | low | volume | amount "
                "| exchange | change_pct |")
        sep = "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
        return "\n".join([head, sep] + rows)

    def test_split_by_code_and_drop_forming_bar(self):
        """多代码一次返回时按 code 拆开，并丢掉尚未收盘的当根。"""
        closed = (self.now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        forming = (self.now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        stdout = self._table([
            f"| sh601318 | {forming} | 54.4 | 54.5 | 54.6 | 54.3 | 100 | 1000 | 6.52 | 0.1 |",
            f"| sh601318 | {closed} | 54.0 | 54.2 | 54.6 | 53.9 | 200 | 2000 | 6.52 | 0.2 |",
            f"| hk00700 | {closed} | 340 | 341 | 342 | 339 | 300 | 3000 | 4.4 | 0.3 |",
        ])
        frames = self.fetcher._parse_table(
            stdout, "m60", {"sh601318": "601318", "hk00700": "hk00700"})

        self.assertEqual(set(frames), {"601318", "hk00700"})
        self.assertEqual(len(frames["601318"]), 1, "未来时间的当根必须被丢掉")
        self.assertEqual(str(frames["601318"]["time"].iloc[-1]), closed)
        # exchange 是千分位数值，m60 要换算成百分数
        self.assertAlmostEqual(float(frames["601318"]["turnover"].iloc[-1]), 0.0652)

    def test_single_code_without_code_column(self):
        closed = (self.now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        stdout = ("| date | open | last | high | low | volume | amount | exchange | change_pct |\n"
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
                  f"| {closed} | 1 | 2 | 3 | 0.5 | 10 | 20 | 1.0 | 0.1 |")
        frames = self.fetcher._parse_table(stdout, "m60", {"sh601318": "601318"})
        self.assertEqual(list(frames), ["601318"])


class IsFreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = kline_cache.KlineCache(root=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, last_time: str):
        p = Path(self.tmp.name) / "601318_m60.csv"
        p.write_text("time,open,close,high,low,turnover,volume\n"
                     f"{last_time},1,2,3,0.5,0.1,10\n" +
                     "\n".join(f"2026-11-20 10:30:00,1,2,3,0.5,0.1,10" for _ in range(25)),
                     encoding="utf-8")

    def test_fresh_when_cache_covers_expected_bar(self):
        self._write("2026-11-26 11:30:00")
        with mock.patch.object(kline_cache, "expected_last_bar",
                               lambda *a, **k: datetime(2026, 11, 26, 11, 30)):
            self.assertTrue(self.cache.is_fresh("601318", "m60"))

    def test_stale_when_new_bar_closed(self):
        self._write("2026-11-26 11:30:00")
        with mock.patch.object(kline_cache, "expected_last_bar",
                               lambda *a, **k: datetime(2026, 11, 26, 14, 0)):
            self.assertFalse(self.cache.is_fresh("601318", "m60"))

    def test_missing_cache_is_never_fresh(self):
        with mock.patch.object(kline_cache, "expected_last_bar",
                               lambda *a, **k: datetime(2026, 11, 26, 14, 0)):
            self.assertFalse(self.cache.is_fresh("300750", "m60"))


if __name__ == "__main__":
    unittest.main()
