"""配置加载、日志、交易时间与 K 线闭合校验。"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import date, datetime, time as dtime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent
# 60 分钟收线时点按市场区分：A 股 4 根/日，港股 7 根/日（9:30-12:00、13:00-16:00）
CLOSE_LABELS = {
    "cn": ("10:30", "11:30", "14:00", "15:00"),
    "hk": ("10:30", "11:30", "12:00", "13:00", "14:00", "15:00", "16:00"),
}
CLOSE_LABELS_60M = CLOSE_LABELS["cn"]   # 兼容旧引用
HK_CACHE = PROJECT_DIR / "data" / "hk_trade_dates.json"
HK_CACHE_TTL = 12 * 3600   # 恒指日历缓存半天，够覆盖盘中判断
# 日线收盘后数据稳定时间：港股晚一小时
DAILY_CLOSE = {"cn": "15:05", "hk": "16:05"}
DAILY_CLOSE_HHMM = DAILY_CLOSE["cn"]


def sh_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def load_config(path: str | Path | None = None) -> dict:
    cfg_path = Path(path) if path else PROJECT_DIR / "config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {cfg_path}")
    with cfg_path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("配置文件顶层必须是映射结构")
    return cfg


def setup_logging(cfg: dict) -> logging.Logger:
    level = getattr(logging, str(cfg.get("general", {}).get("log_level", "INFO")).upper(), logging.INFO)
    log_dir = PROJECT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("wave-monitor")
    logger.setLevel(level)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    file_handler = RotatingFileHandler(log_dir / "monitor.log", maxBytes=2_000_000,
                                       backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger


def load_secret(env_var: str) -> str | None:
    """按 ~/.env -> 项目 .env -> 进程环境变量的顺序读取密钥，不打印任何密钥内容。"""
    for path in (Path.home() / ".env", PROJECT_DIR / ".env"):
        if path.exists():
            values = dotenv_values(path)
            value = values.get(env_var)
            if value:
                return str(value).strip()
    value = os.environ.get(env_var)
    return value.strip() if value else None


def is_trading_day(now: datetime | None = None) -> bool:
    """优先用 akshare 交易日历（本地缓存 7 天），失败时退化为周一至周五。"""
    now = now or sh_now()
    cache_path = PROJECT_DIR / "data" / "trade_dates.json"
    dates: set[str] | None = None

    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < 7 * 86400:
        try:
            dates = set(json.loads(cache_path.read_text(encoding="utf-8"))["dates"])
        except (ValueError, KeyError, OSError):
            dates = None

    if dates is None:
        try:
            import akshare as ak
            func = getattr(ak, "calendar_trade_date_hist_sina", None) or getattr(ak, "tool_trade_date_hist_sina")
            df = func()
            dates = {str(v)[:10] for v in df.iloc[:, 0].tolist()}
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"dates": sorted(dates)}), encoding="utf-8")
        except Exception:
            return now.weekday() < 5  # 日历不可用时退化为工作日判断

    return now.strftime("%Y-%m-%d") in dates


def hk_trade_dates(force: bool = False) -> set[str] | None:
    """港股交易日集合：取恒生指数日线的日期（指数每个交易日必有数据）。

    优先本机 westock CLI（与行情同源），缓存 12 小时。
    返回 None 表示无法获取，调用方需退化处理。
    """
    if not force and HK_CACHE.exists() and time.time() - HK_CACHE.stat().st_mtime < HK_CACHE_TTL:
        try:
            return set(json.loads(HK_CACHE.read_text(encoding="utf-8"))["dates"])
        except (ValueError, KeyError, OSError):
            pass

    cli = os.path.expanduser(os.environ.get("WESTOCK_CLI", "~/.local/bin/westock"))
    if not os.path.exists(cli):
        return None
    try:
        proc = subprocess.run(
            [cli, "kline", "--code", "hkHSI", "--period", "day", "--limit", "400"],
            capture_output=True, text=True, timeout=40,
        )
        if proc.returncode != 0:
            return None
        dates = set()
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cell = line.strip("|").split("|")[0].strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cell):
                dates.add(cell)
        if not dates:
            return None
        HK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        HK_CACHE.write_text(json.dumps({"dates": sorted(dates), "source": "hkHSI"},
                                       ensure_ascii=False), encoding="utf-8")
        return dates
    except Exception:  # noqa: BLE001 - 取数失败一律退化为 None
        return None


def is_hk_trading_day(now: datetime | None = None) -> bool | None:
    """港股是否开市。None = 日历取不到，无法判断。"""
    now = now or sh_now()
    dates = hk_trade_dates()
    if dates is None:
        return None
    return now.strftime("%Y-%m-%d") in dates


def is_any_trading_day(now: datetime | None = None) -> bool:
    """A 股或港股任一开市即视为需要扫描（两个市场的标的都在同一轮里跑）。"""
    now = now or sh_now()
    cn = is_trading_day(now)
    hk = is_hk_trading_day(now)
    if hk is None:
        return cn
    return cn or hk


def turnover_multiple(df, window: int = 20) -> float:
    """当根换手 ÷ 前 window 根换手中位数（不含当根），即「量能倍数」。

    用于在推送里标注信号强弱：实测 ≥2.5 倍的买点命中率 75~83%，
    1.0~2.5 倍只有 45~47%（2026-11-25 六序列量化）。
    """
    if df is None or len(df) < 2 or "turnover" not in df:
        return 0.0
    t = df["turnover"]
    med = t.iloc[-(window + 1):-1].median()
    try:
        return round(float(t.iloc[-1]) / float(med), 2) if med else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def volume_tier(multiple: float) -> tuple[str, str]:
    """量能倍数 → (文字档位, 语气)。语气用企微 markdown 支持的 info/comment/warning。"""
    if multiple <= 0:
        return "", "comment"
    if multiple >= 2.5:
        return "放量", "warning"
    if multiple >= 1.5:
        return "温和放量", "info"
    if multiple >= 1.0:
        return "平量", "comment"
    return "缩量", "comment"


def is_today_bar(bar_time, now: datetime | None = None) -> bool:
    """K 线时间是否属于今天。用于拦掉数据滞后/节假日残留的旧信号，避免推过期提醒。"""
    now = now or sh_now()
    return str(bar_time)[:10] == now.strftime("%Y-%m-%d")


def trading_day_status(now: datetime | None = None) -> dict:
    """三态明细，供日志与排查使用。"""
    now = now or sh_now()
    cn = is_trading_day(now)
    hk = is_hk_trading_day(now)
    return {"date": now.strftime("%Y-%m-%d"), "cn": cn, "hk": hk,
            "any": cn or bool(hk)}


def market_of(code) -> str:
    """按代码前缀判断市场：hk 开头为港股，其余按 A 股。"""
    return "hk" if str(code).strip().lower().startswith("hk") else "cn"


BAR_GRACE_SECONDS = 120   # 收线后给数据源留的出数时间；未过这段视为尚未收线


def market_open_on(day: date, market: str = "cn") -> bool:
    """某个自然日该市场是否开市；港股日历取不到时退化为工作日判断。"""
    probe = datetime.combine(day, dtime(15, 0))
    if market == "hk":
        value = is_hk_trading_day(probe)
        return day.weekday() < 5 if value is None else bool(value)
    return is_trading_day(probe)


def prev_trading_day(day: date, market: str = "cn") -> date:
    """上一个交易日（不含 day 本身）。"""
    cursor = day - timedelta(days=1)
    for _ in range(15):
        if market_open_on(cursor, market):
            return cursor
        cursor -= timedelta(days=1)
    return cursor


def expected_last_bar(period: str, market: str = "cn",
                     now: datetime | None = None) -> datetime | None:
    """此刻「本应已经收线」的最后一根 K 线时间（naive 本地时间）。

    只用于判断本地缓存是否已经覆盖最新一根已收盘 K 线——覆盖了就不必再请求上游，
    真正是否可用仍由 validate_last_bar 决定。返回 None 表示无法判断。
    """
    now = (now or sh_now()).replace(tzinfo=None)
    today = now.date()

    if period == "m60":
        labels = CLOSE_LABELS.get(market, CLOSE_LABELS["cn"])
        last_hhmm = datetime.strptime(labels[-1], "%H:%M").time()
        if market_open_on(today, market):
            cutoff = now - timedelta(seconds=BAR_GRACE_SECONDS)
            passed = [lab for lab in labels if cutoff.strftime("%H:%M") >= lab]
            if passed:
                return datetime.combine(today, datetime.strptime(passed[-1], "%H:%M").time())
        return datetime.combine(prev_trading_day(today, market), last_hhmm)

    close_hhmm = DAILY_CLOSE.get(market, DAILY_CLOSE["cn"])
    if market_open_on(today, market) and now.strftime("%H:%M") >= close_hhmm:
        return datetime.combine(today, dtime(0, 0))
    return datetime.combine(prev_trading_day(today, market), dtime(0, 0))


def validate_last_bar(df, period: str, now: datetime | None = None,
                      grace_minutes: int = 1, market: str = "cn"):
    """剔除未闭合 K 线，返回 (是否可用, 说明, 清洗后的数据)。

    60 分钟收线时点按 market 取（A 股 10:30/11:30/14:00/15:00；
    港股额外有 12:00/13:00/16:00），且需已过收线时刻；
    日线在收盘后稳定时间（A 股 15:05、港股 16:05）前丢弃当日包。
    """
    from indicator import normalize_columns  # 局部导入，避免模块间循环依赖

    now = (now or sh_now()).replace(tzinfo=None)
    data = normalize_columns(df)
    if data.empty:
        return False, "行情为空", data

    notes: list[str] = []
    reason = ""
    for _ in range(2):
        if data.empty:
            break
        last = data.iloc[-1]["time"]
        if period == "m60":
            label = last.strftime("%H:%M")
            volume = data.iloc[-1]["volume"]
            if label not in CLOSE_LABELS.get(market, CLOSE_LABELS["cn"]):
                reason = f"最后一根 {last} 不是标准收线时点"
            elif pd_volume_invalid(volume):
                reason = f"最后一根 {last} 成交量为 0，视为未闭合"
            elif last.date() == now.date() and now < last + timedelta(minutes=grace_minutes):
                reason = f"最后一根 {last} 尚未确认收盘"
            elif last.date() < now.date() and now.strftime("%H:%M") >= "10:30":
                reason = f"行情滞后，最后一根为 {last.date()}"
            else:
                return True, "；".join(notes) or "OK", data
        else:
            if last.date() == now.date() and now.strftime("%H:%M") < DAILY_CLOSE.get(market, DAILY_CLOSE["cn"]):
                reason = f"今日日线 {last.date()} 尚未收盘，已剔除"
            else:
                return True, "；".join(notes) or "OK", data

        notes.append(reason)
        data = data.iloc[:-1].reset_index(drop=True)

    return False, "；".join(notes + [reason]) or "无已闭合 K 线", data


def pd_volume_invalid(value) -> bool:
    try:
        return value is None or float(value) <= 0
    except (TypeError, ValueError):
        return True
