"""配置加载、日志、交易时间与 K 线闭合校验。"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent
CLOSE_LABELS_60M = ("10:30", "11:30", "14:00", "15:00")
DAILY_CLOSE_HHMM = "15:05"  # 日线收盘后数据稳定时间


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


def validate_last_bar(df, period: str, now: datetime | None = None, grace_minutes: int = 1):
    """剔除未闭合 K 线，返回 (是否可用, 说明, 清洗后的数据)。

    60 分钟最后一根的时间标签必须属于 {10:30,11:30,14:00,15:00}，且已过收线时刻；
    日线在 15:05 前丢弃当日包（数据源会在盘中给出未收盘的当日 K 线）。
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
            if label not in CLOSE_LABELS_60M:
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
            if last.date() == now.date() and now.strftime("%H:%M") < DAILY_CLOSE_HHMM:
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
