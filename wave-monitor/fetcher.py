"""行情拉取：westock 多代码批量取数 + 本地增量缓存，含列名适配与限频兜底。

上游（腾讯行情 / 东财）按 IP + 接口做滑动窗口与并发频控，请求数才是关键变量：
同一周期的标的合并成一条命令、缓存覆盖最新收线时直接不发请求。
"""

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
from kline_cache import KlineCache
from utils import BAR_GRACE_SECONDS, DAILY_CLOSE, market_of, sh_now

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
        # 单条命令最多拼多少只代码。上游按 IP 频控，批量把请求数直接除以 N，
        # 是本模块降低请求量的主要手段（17 只 → 1 条命令）。
        self.batch_size = max(1, int(fetch_cfg.get("batch_size", 20)))
        # 是否允许降级到 akshare/东财。默认关闭：只走 westock，失败就跳过，
        # 避免降级通道把标的一次性打到限流（2026-11-25 用户要求）
        self.allow_fallback = bool(fetch_cfg.get("fallback_to_akshare", False))
        self.westock_cli = os.path.expanduser(
            str(fetch_cfg.get("westock_cli", "~/.local/bin/westock")))
        self.logger = logger or logging.getLogger("wave-monitor")
        self._last_request = 0.0
        self._degraded_off = False   # 东财/akshare 熔断：一旦限流，本轮不再降级
        self._westock_limited = False  # 上游限频：一旦触发，本轮剩余请求全部改用缓存
        self.cache = KlineCache()      # 本地 K 线增量缓存，压降每轮请求行数
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
        """单只取数（保留给回测/预热等单点调用）；内部走批量路径。"""
        return self.fetch_batch([str(symbol)], period)[str(symbol)]

    def fetch_batch(self, symbols: list[str], period: str,
                    force: bool = False) -> dict[str, tuple[pd.DataFrame, str]]:
        """批量取数：同一周期的全部标的合并成 1~2 条 westock 命令。

        上游（腾讯行情 / 东财）是按 IP + 接口做滑动窗口与并发频控，逐只拆开查询会把
        请求数放大 N 倍。这里做三件事压请求量：
          1) 本地缓存已覆盖「本应已收线」的最后一根 → 直接吃缓存，0 请求；
          2) 必须请求时多代码逗号拼接，一次命令取多只；
          3) 首轮（无缓存）与增量分成两批，避免为少数新标的把全量窗口拉一遍。
        """
        codes = [str(s) for s in symbols]
        out: dict[str, tuple[pd.DataFrame, str]] = {}
        pending: dict[str, tuple] = {}

        for code in codes:
            cached = self.cache.load(code, period)
            if cached is None or force or not self.cache.is_fresh(code, period):
                pending[code] = self.cache.plan(code, period)
            else:
                out[code] = (cached.tail(self.bars).reset_index(drop=True), "cache")

        if not pending:
            self.logger.info("%s 共 %d 只，均已覆盖最新收线，本轮 0 请求", period, len(codes))
            return out

        if self._westock_limited:
            self.logger.info("%s 共 %d 只：本轮已限频，改用本地缓存", period, len(pending))
            return self._from_cache(pending, period, out)

        if not self.use_westock:
            for code in pending:
                df, source = self._try_akshare(code, period)
                out[code] = (df, source)
            return out

        frames, calls = self._westock_kline_batch(list(pending), period, pending)
        rows = 0
        for code, (_start, _end, cached) in pending.items():
            fresh = frames.get(code)
            if fresh is not None and not fresh.empty:
                out[code] = (self.cache.merge(code, period, fresh, self.bars, cached), "westock")
                rows += len(fresh)
            elif cached is not None:
                self.logger.warning("%s %s: 未取到增量，用本地缓存 %d 根兜底",
                                    code, period, len(cached))
                out[code] = (cached.tail(self.bars).reset_index(drop=True), "cache")
            elif self.allow_fallback and not self._degraded_off:
                # 仅在显式开启降级时逐只走 akshare/东财（默认关闭：会把同一个 IP 再打一遍）
                alt, alt_source = self._try_akshare(code, period)
                if alt is None or alt.empty:
                    alt = self._try_eastmoney(code, period)
                    alt_source = "eastmoney"
                out[code] = (alt if alt is not None else pd.DataFrame(), alt_source)
            else:
                out[code] = (pd.DataFrame(), "skipped" if self._westock_limited else "westock")

        self.logger.info("%s 批量取数：%d 只 / %d 次请求，新增 %d 行（%d 只直接用缓存）",
                         period, len(pending), calls, rows, len(codes) - len(pending))
        return out

    def _from_cache(self, pending: dict[str, tuple], period: str,
                    out: dict[str, tuple[pd.DataFrame, str]]):
        for code, (_start, _end, cached) in pending.items():
            if cached is not None:
                out[code] = (cached.tail(self.bars).reset_index(drop=True), "cache")
            else:
                out[code] = (pd.DataFrame(), "skipped")
        return out

    @staticmethod
    def _westock_code(symbol: str) -> str:
        code = str(symbol).strip()
        if code.startswith(("sh", "sz", "bj", "hk", "us")):
            return code
        if code.startswith(("5", "6", "9")):
            # 5 开头是上交所基金/ETF（深市 ETF 为 15/16 开头），必须走 sh
            return "sh" + code
        if code.startswith(("4", "8")):
            return "bj" + code
        return "sz" + code

    def _westock_kline_batch(self, codes: list[str], period: str,
                             plans: dict[str, tuple]):
        """按「增量 / 全量」分桶后切块发请求，返回 ({code: 行情}, 请求次数)。"""
        if not os.path.exists(self.westock_cli):
            self.logger.warning("westock CLI 不存在: %s", self.westock_cli)
            return {}, 0

        buckets: dict[str, list[str]] = {"inc": [], "full": []}
        for code in codes:
            _start, _end, cached = plans[code]
            buckets["inc" if cached is not None else "full"].append(code)

        frames: dict[str, pd.DataFrame] = {}
        calls = 0
        for key in ("inc", "full"):
            bucket = buckets[key]
            for i in range(0, len(bucket), self.batch_size):
                chunk = bucket[i:i + self.batch_size]
                # 同一批共用区间：起点取最早、终点取最晚，多拉的几行不影响结果
                start = min(plans[c][0] for c in chunk)
                end = max(plans[c][1] for c in chunk)
                calls += 1
                frames.update(self._run_kline(chunk, period, start, end))
                if self._westock_limited:
                    return frames, calls
        return frames, calls

    def _run_kline(self, codes: list[str], period: str,
                   start: date, end: date) -> dict[str, pd.DataFrame]:
        """一条命令取多只 K 线（`westock kline a,b,c`），按 code 列拆回每只。"""
        wmap = {self._westock_code(c): c for c in codes}
        args = [self.westock_cli, "kline", ",".join(wmap),
                "--period", "day" if period == "daily" else "m60",
                "--start", start.isoformat(), "--end", end.isoformat()]
        if period == "daily":
            args += ["--fq", "qfq"]
        try:
            self._throttle()
            proc = subprocess.run(args, capture_output=True, text=True, timeout=90)
            if proc.returncode != 0:
                raise FetchError(f"westock 退出码 {proc.returncode}: {proc.stderr[:200]}")
            return self._parse_table(proc.stdout, period, wmap)
        except Exception as exc:  # noqa: BLE001 - CLI 异常类型不固定，统一降级到缓存
            msg = str(exc)
            if "服务限频" in msg or "1620053006" in msg:
                self._westock_limited = True
                self.logger.warning(
                    "westock 服务限频（上游按 IP/接口频控），本轮剩余请求全部改用缓存")
            else:
                self.logger.warning("westock 批量取数失败(%s)", exc)
            return {}

    def _parse_table(self, stdout: str, period: str,
                     wmap: dict[str, str]) -> dict[str, pd.DataFrame]:
        lines = [line.strip() for line in stdout.splitlines() if line.strip().startswith("|")]
        if len(lines) < 3:
            raise FetchError(f"westock 返回空表格: {stdout[:200]}")
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

        df = pd.DataFrame(rows, columns=header)
        if "code" in df.columns:
            # 多代码批量返回会多一列 code；单代码没有，统一在这里拆开
            grouped = [(wmap.get(str(k), str(k)), g)
                       for k, g in df.groupby("code", sort=False)]
        else:
            grouped = [(next(iter(wmap.values()), None), df)]

        out: dict[str, pd.DataFrame] = {}
        for code, group in grouped:
            if not code:
                continue
            frame = self._to_kline(group.drop(columns=["code"], errors="ignore"),
                                   period, market_of(code))
            if frame is not None and not frame.empty:
                out[code] = frame
        return out

    def _to_kline(self, df: pd.DataFrame, period: str, market: str = "cn") -> pd.DataFrame:
        """归一化并丢掉「还在形成中」的当根——上游会把未收盘的当根一起返回。

        缓存里只留已收盘 K 线，缓存新鲜度判断才能做到零误差：
        最后一根等于「本应已收线」的那一刻，就说明没有新数据可取。
        """
        frame = df.rename(columns={"date": "time", "last": "close", "exchange": "turnover"})
        if period == "m60":
            # westock 分钟线 exchange 是千分位数值（48.65 = 0.4865%）
            frame["turnover"] = pd.to_numeric(frame["turnover"], errors="coerce") / 100.0
        frame = normalize_columns(frame)

        now = sh_now().replace(tzinfo=None)
        if period == "daily":
            close_hhmm = DAILY_CLOSE.get(market, DAILY_CLOSE["cn"])
            if now.strftime("%H:%M") < close_hhmm:
                frame = frame[frame["time"].dt.date < now.date()]
        else:
            frame = frame[frame["time"] <= now - timedelta(seconds=BAR_GRACE_SECONDS)]
        return frame.tail(self.bars).reset_index(drop=True)

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
                self._degraded_off = True
                self.logger.warning("%s %s: 检测到东财 push2 限流，本轮后续标的全部跳过降级通道",
                                    symbol, period)
            else:
                self.logger.warning("%s %s: akshare 重试 %d 次仍失败(%s)，降级东财",
                                    symbol, period, exc.last_attempt.attempt_number, cause)
        except Exception as exc:  # noqa: BLE001 - 限流异常可能不经 RetryError 直接抛出
            if _is_push2_rate_limit(exc):
                self._degraded_off = True
                self.logger.warning("%s %s: 检测到东财 push2 限流，本轮后续标的全部跳过降级通道",
                                    symbol, period)
            else:
                self.logger.warning("%s %s: akshare 失败(%s)", symbol, period, exc)
        return None, "akshare"

    def _try_eastmoney(self, symbol: str, period: str) -> pd.DataFrame:
        if self._degraded_off:
            return pd.DataFrame()
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
