"""日线与 60 分钟周期隔离单测：一轮扫描只取自己周期的行情（不联网）。"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402

SYMBOLS = [{"code": "601318", "name": "中国平安"}, {"code": "hk00700", "name": "腾讯集团"}]


def _cfg(daily_enabled: bool = True) -> dict:
    return {
        "periods": {
            "m60": {"enabled": True, "push": True},
            "daily": {"enabled": daily_enabled, "push": daily_enabled},
        },
        "symbols": SYMBOLS,
    }


class PeriodIsolationTests(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple[str, bool]] = []
        self.logger = logging.getLogger("test-isolation")
        self.logger.addHandler(logging.NullHandler())
        self.logger.propagate = False

    def _run(self, period_filter, cfg=None) -> list[str]:
        """跑一遍 _scan，返回 fetch_batch 被调用的周期序列。"""
        cfg = cfg or _cfg()

        class FakeFetcher:
            def __init__(self, *_a, **_kw):
                pass

            def fetch_batch(self, codes, period, force=False):
                self_calls.append(period)
                return {c: ("", "cache") for c in codes}

        self_calls: list[str] = []
        fetcher_cls = mock.Mock(side_effect=lambda *a, **k: FakeFetcher())
        notifier = mock.Mock(ready=True)

        with mock.patch.object(main, "MarketFetcher", fetcher_cls), \
             mock.patch.object(main, "Notifier", lambda *a, **k: notifier), \
             mock.patch.object(main, "StateStore", lambda *a, **k: mock.Mock()), \
             mock.patch.object(main.scan_log, "record_run", lambda *a, **k: None):
            main._scan(cfg, self.logger, SYMBOLS, period_filter, False)
        return self_calls

    def test_m60_run_never_fetches_daily(self):
        """60 分钟轮次只取 m60，不取日线（原日线确认已删除）。"""
        self.assertEqual(self._run("m60"), ["m60"])

    def test_daily_run_never_fetches_m60(self):
        """日线轮次只取 daily，不取 60 分钟。"""
        self.assertEqual(self._run("daily"), ["daily"])

    def test_daily_disabled_skips_daily_period(self):
        """日线关闭时，整轮运行不碰 daily。"""
        self.assertEqual(self._run(None, _cfg(daily_enabled=False)), ["m60"])


class NotifierConfirmTests(unittest.TestCase):
    def test_no_daily_confirm_line_without_tag(self):
        """没有日线确认标签时，消息里不应出现「日线：未取到」这类占位行。"""
        from notifier import format_signal

        text = format_signal({
            "signal": "BUY", "period": "m60", "name": "中国平安", "symbol": "601318",
            "time": "2026-11-26 11:30", "close": 50.0, "ma7": 49.8, "ma13": 49.5,
            "rho": 0.994, "signal_mode": "ma7", "source": "westock",
        })
        self.assertNotIn("未取到", text)
        self.assertNotIn("日线", text)

    def test_daily_confirm_kept_when_provided(self):
        from notifier import format_signal

        text = format_signal({
            "signal": "SELL", "period": "m60", "name": "中国平安", "symbol": "601318",
            "time": "2026-11-26 11:30", "close": 50.0, "ma7": 49.8, "ma13": 49.5,
            "rho": 1.0, "signal_mode": "ma7", "source": "westock",
            "daily_confirm": "日线二次确认：B 买点（持有）",
        })
        self.assertIn("日线二次确认", text)


if __name__ == "__main__":
    unittest.main()
