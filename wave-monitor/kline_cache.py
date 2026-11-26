"""K 线增量缓存。

思路来自 chan-trading-decision 的 `fetch_kline_cached.py`：历史 K 线落本地 CSV，
每次只向接口要「最近几天」的增量，合并后写回。

目的不是省时间，而是把**每轮扫描的返回行数**从上百行压到几十行——
westock 的「服务限频」（code=1620053006）是配额型而非频率型：缠论任务 0.8 秒间隔
却不限流，正是因为它每只每轮只拉 2~3 行增量；我们此前每轮全量拉 120 根，
累计行数是它的 20 倍，才会在第 5~11 只就撞墙。

缓存文件：`data/kline_cache/{code}_{period}.csv`，列为标准 K 线列。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from utils import DAILY_CLOSE, expected_last_bar, market_of, sh_now

PROJECT_DIR = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_DIR / "data" / "kline_cache"

# 增量回看天数：日线要覆盖长假，分钟线几天足够（重叠是为了覆盖数据源的临时修正）
LOOKBACK = {"daily": 12, "m60": 6}
# 首次（无缓存时）全量回看天数
FULL_LOOKBACK = {"daily": 300, "m60": 40}
# 缓存行数低于这个值视为无效，重新全量
# 门槛别设太高：westock 的 m60 一次只给 36 根左右，设 40 会永远判缓存无效
MIN_ROWS = {"daily": 60, "m60": 20}


class KlineCache:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else CACHE_DIR
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, code: str, period: str) -> Path:
        return self.root / f"{code}_{period}.csv"

    def load(self, code: str, period: str) -> pd.DataFrame | None:
        p = self._path(code, period)
        if not p.exists():
            return None
        try:
            df = pd.read_csv(p)
        except (OSError, ValueError):
            return None
        if df.empty or "time" not in df.columns:
            return None
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
        if len(df) < MIN_ROWS.get(period, 30):
            return None
        return df

    def plan(self, code: str, period: str, today: date | None = None):
        """返回 (start, end, cached)。

        有缓存 → 只拉「最后日期 - LOOKBACK」到今天的增量，cached 为旧数据；
        无缓存 → 拉全量窗口，cached 为 None。
        """
        today = today or date.today()
        cached = self.load(code, period)
        if cached is None:
            return today - timedelta(days=FULL_LOOKBACK.get(period, 90)), today, None
        last = cached["time"].max().date()
        return last - timedelta(days=LOOKBACK.get(period, 6)), today, cached

    def is_fresh(self, code: str, period: str, now: datetime | None = None) -> bool:
        """本地缓存是否已经覆盖「本应已收线」的最后一根 K 线。

        覆盖了就没有新数据可取，直接吃缓存、不发请求——这是把每轮请求数
        从「每只一次」压到「每周期一次（甚至 0 次）」的关键。
        日线特殊：盘中写入的当日包不算收盘数据，收盘后必须重取一次。
        """
        cached = self.load(code, period)
        if cached is None or cached.empty:
            return False
        last = pd.Timestamp(cached["time"].max())
        market = market_of(code)
        expected = expected_last_bar(period, market, now)
        if expected is None:
            return True
        if last < pd.Timestamp(expected):
            return False
        if period != "daily":
            return True

        current = (now or sh_now()).replace(tzinfo=None)
        if last.date() != current.date() or expected.date() != current.date():
            return True
        close_hhmm = DAILY_CLOSE.get(market, DAILY_CLOSE["cn"])
        close_at = datetime.combine(current.date(),
                                    datetime.strptime(close_hhmm, "%H:%M").time())
        try:
            written = datetime.fromtimestamp(self._path(code, period).stat().st_mtime)
        except OSError:
            return False
        return written >= close_at

    def merge(self, code: str, period: str, fresh: pd.DataFrame,
              bars: int, cached: pd.DataFrame | None = None) -> pd.DataFrame:
        """把增量合并进缓存并回写，返回最近 bars 根（升序）。"""
        if cached is None:
            cached = self.load(code, period)
        if cached is None:
            merged = fresh.copy()
        else:
            merged = pd.concat([cached, fresh], ignore_index=True)
        merged = (merged.drop_duplicates(subset=["time"], keep="last")
                        .sort_values("time").reset_index(drop=True))
        # 只留已收盘的 K 线：上游会把正在形成的当根一起返回，历史缓存里也可能残留，
        # 留着会让「缓存已最新」的判断失准，也会让指标用到半根数据
        expected = expected_last_bar(period, market_of(code))
        if expected is not None:
            merged = merged[merged["time"] <= pd.Timestamp(expected)].reset_index(drop=True)
        # 缓存留冗余，方便下次继续算长周期指标
        merged = merged.tail(max(bars * 2, 240)).reset_index(drop=True)
        self._save(code, period, merged)
        return merged.tail(bars).reset_index(drop=True)

    def _save(self, code: str, period: str, df: pd.DataFrame) -> None:
        p = self._path(code, period)
        tmp = p.with_suffix(".csv.tmp")
        try:
            df.to_csv(tmp, index=False)
            tmp.replace(p)
        except OSError:
            pass

    def stats(self, code: str, period: str) -> dict:
        df = self.load(code, period)
        if df is None:
            return {"cached": 0}
        return {"cached": len(df), "last": str(df["time"].max())[:16]}
