"""行情拉取：akshare 主通道 + 东财 push2his 降级，含列名适配、限速与退避重试。"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from tenacity import (RetryError, Retrying, retry_if_exception, retry_if_exception_type,
                      stop_after_attempt, wait_chain, wait_fixed)

from indicator import normalize_columns
from utils import sh_now

EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"  # 时间/开/收/高/低/量/额/振幅/涨跌幅/涨跌额/换手率
KLINE_COLUMNS = ["time", "open", "close", "high", "low", "volume", "amount",
                 "amplitude", "pct_change", "change", "turnover"]


class FetchError(RuntimeError):
    """行情获取失败。"""


def _is_push2_rate_limit(exc: BaseException) -> bool:
    """东财 push2 限流时重试只会延长封禁，识别后立刻停止重试。"""
    text = f"{type(exc).__name__} {exc}"
    return ("Push2RateLimitError" in text
            or ("push2" in text.lower() and "限流" in text)
            or "push2 行情服务器" in text)


class MarketFetcher:
    def __init__(self, cfg: dict, logger: logging.Logger | None = None):
        fetch_cfg = cfg.get("fetch", {}) or {}
        self.interval = float(fetch_cfg.get("interval_seconds", 0.8))
        self.retries = int(fetch_cfg.get("retries", 3))
        self.backoff = [float(v) for v in fetch_cfg.get("backoff_seconds", [1, 3, 8])] or [1, 3, 8]
        self.bars = int(fetch_cfg.get("history_bars", 120))
        self.use_westock = bool(fetch_cfg.get("use_westock", True))
        self.westock_cli = os.path.expanduser(
            str(fetch_cfg.get("westock_cli", "~/.local/bin/westock")))
        self.logger = logger or logging.getLogger("wave-monitor")
        self._last_request = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        })

    def _throttle(self) -> None:
        wait = self.interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _retrying(self) -> Retrying:
        return Retrying(
            stop=stop_after_attempt(self.retries),
            wait=wait_chain(*[wait_fixed(v) for v in self.backoff]),
            retry=retry_if_exception_type(Exception) & retry_if_exception(
                lambda exc: not _is_push2_rate_limit(exc)),
            reraise=False,  # 由调用方捕获 RetryError 并降级
        )

    def fetch(self, symbol: str, period: str) -> tuple[pd.DataFrame, str]:
        """返回 (归一化行情, 数据来源)。优先 westock，失败再降级 akshare / 东财。"""
        if self.use_westock:
            raw, source = self._try_westock(symbol, period)
            if raw is not None and not raw.empty:
                return raw, source
            self.logger.warning("%s %s: westock 无数据，降级 akshare", symbol, period)

        raw, source = self._try_akshare(symbol, period)
        if raw is not None and not raw.empty:
            return raw, source
        self.logger.warning("%s %s: akshare 无数据，尝试东财备用接口", symbol, period)
        raw = self._try_eastmoney(symbol, period)
        return raw, "eastmoney"

    @staticmethod
    def _westock_code(symbol: str) -> str:
        code = str(symbol).strip()
        if code.startswith(("sh", "sz", "bj", "hk", "us")):
            return code
        if code.startswith(("6", "9")):
            return "sh" + code
        if code.startswith(("4", "8")):
            return "bj" + code
        return "sz" + code

    def _try_westock(self, symbol: str, period: str):
        """调用本机 westock CLI 取 K 线：日线 qfq；m60 近一个月且强制不复权。"""
        if not os.path.exists(self.westock_cli):
            self.logger.warning("westock CLI 不存在: %s，降级 akshare", self.westock_cli)
            return None, "westock"

        code = self._westock_code(symbol)
        if period == "m60":
            today = date.today()
            args = [self.westock_cli, "kline", code, "--period", "m60",
                    "--start", (today - timedelta(days=28)).isoformat(),
                    "--end", today.isoformat()]
        else:
            args = [self.westock_cli, "kline", code, "--period", "day",
                    "--limit", str(self.bars), "--fq", "qfq"]

        try:
            self._throttle()
            proc = subprocess.run(args, capture_output=True, text=True, timeout=45)
            if proc.returncode != 0:
                raise FetchError(f"westock 退出码 {proc.returncode}: {proc.stderr[:200]}")
            lines = [line.strip() for line in proc.stdout.splitlines()
                     if line.strip().startswith("|")]
            if len(lines) < 3:
                raise FetchError(f"westock 返回空表格: {proc.stdout[:200]}")
            header = [c.strip() for c in lines[0].strip("|").split("|")]
            rows = []
            for line in lines[2:]:
                cells = [c.strip() for c in line.strip("|").split("|")]
                if all(c.startswith("-") for c in cells):
                    continue
                if len(cells) == len(header):
                    rows.append(cells)
            if not rows:
                raise FetchError("westock 表格无数据行")

            df = pd.DataFrame(rows, columns=header).rename(
                columns={"date": "time", "last": "close", "exchange": "turnover"})
            if period == "m60":
                # westock 分钟线 exchange 是千分位数值（48.65 = 0.4865%）
                df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce") / 100.0
            result = normalize_columns(df).tail(self.bars).reset_index(drop=True)
            self.logger.info("%s %s: westock 拉取 %d 根 K 线", symbol, period, len(result))
            return result, "westock"
        except Exception as exc:  # noqa: BLE001 - CLI 异常类型不固定，统一降级
            self.logger.warning("%s %s: westock 失败(%s)，降级 akshare", symbol, period, exc)
            return None, "westock"

    def _try_akshare(self, symbol: str, period: str):
        try:
            import akshare as ak
        except ImportError as exc:
            self.logger.warning("akshare 不可用，直接使用东财备用接口: %s", exc)
            return None, "akshare"

        if period == "m60":
            func = getattr(ak, "stock_zh_a_hist_min_em", None)
            kwargs = {
                "symbol": symbol, "period": "60", "adjust": "qfq",
                "start_date": (sh_now() - timedelta(days=180)).strftime("%Y-%m-%d 00:00:00"),
                "end_date": sh_now().strftime("%Y-%m-%d 23:59:59"),
            }
        else:
            # akshare >= 1.20.46 将 stock_zh_a_hist 更名为 stock_zh_a_hist_em
            func = getattr(ak, "stock_zh_a_hist_em", None) or getattr(ak, "stock_zh_a_hist", None)
            kwargs = {
                "symbol": symbol, "period": "daily", "adjust": "qfq",
                "start_date": (sh_now() - timedelta(days=self.bars * 3 + 60)).strftime("%Y%m%d"),
                "end_date": sh_now().strftime("%Y%m%d"),
            }
        if func is None:
            self.logger.warning("akshare 中找不到对应行情函数，改用东财备用接口")
            return None, "akshare"

        try:
            for attempt in self._retrying():
                with attempt:
                    self._throttle()
                    df = func(**kwargs)
                    if df is None or df.empty:
                        raise FetchError(f"akshare 返回空数据 ({symbol} {period})")
                    result = normalize_columns(df).tail(self.bars).reset_index(drop=True)
                    self.logger.info("%s %s: akshare 拉取 %d 根 K 线", symbol, period, len(result))
                    return result, "akshare"
        except RetryError as exc:
            cause = exc.last_attempt.exception()
            if _is_push2_rate_limit(cause):
                self.logger.warning("%s %s: 检测到东财 push2 限流，停止重试，直接走备用通道",
                                    symbol, period)
            else:
                self.logger.warning("%s %s: akshare 重试 %d 次仍失败(%s)，降级东财",
                                    symbol, period, exc.last_attempt.attempt_number, cause)
        return None, "akshare"

    def _try_eastmoney(self, symbol: str, period: str) -> pd.DataFrame:
        params = {
            "secid": self._secid(symbol),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": EM_FIELDS2,
            "klt": "60" if period == "m60" else "101",
            "fqt": "1",  # 前复权，与 akshare adjust=qfq 一致
            "beg": "0",
            "end": "20500101",
            "lmt": str(self.bars),
        }
        try:
            for attempt in self._retrying():
                with attempt:
                    self._throttle()
                    resp = self._session.get(EASTMONEY_KLINE_URL, params=params, timeout=15)
                    resp.raise_for_status()
                    payload = resp.json().get("data") or {}
                    klines = payload.get("klines") or []
                    if not klines:
                        raise FetchError(f"东财返回空数据 ({symbol} {period})")
                    rows = [line.split(",")[:len(KLINE_COLUMNS)] for line in klines]
                    df = pd.DataFrame(rows, columns=KLINE_COLUMNS[:max(len(r) for r in rows)])
                    result = normalize_columns(df).tail(self.bars).reset_index(drop=True)
                    self.logger.info("%s %s: 东财备用拉取 %d 根 K 线", symbol, period, len(result))
                    return result
        except RetryError as exc:
            raise FetchError(f"{symbol} {period} 双通道均失败: {exc.last_attempt.exception()}") from exc
        raise FetchError(f"{symbol} {period} 双通道均失败")

    @staticmethod
    def _secid(symbol: str) -> str:
        symbol = str(symbol).strip()
        market = "1" if symbol.startswith(("6", "9")) else "0"
        return f"{market}.{symbol}"
