#!/usr/bin/env python3
"""波段助手买点监控主入口：拉数 -> 闭合校验 -> 算信号 -> 去重 -> 推送 -> 日志。

用法：
    python3 main.py --once                 # 单次扫描（launchd 每时点调用）
    python3 main.py --once --period m60    # 只跑 60 分钟
    python3 main.py --once --force         # 忽略去重，便于 dry-run 验证消息
    python3 main.py --backtest --code 300750 --period m60
"""

from __future__ import annotations

import argparse
import logging
import sys

from fetcher import MarketFetcher
from indicator import compute_indicators, latest_signal
from notifier import Notifier
from state_store import StateStore
from utils import is_trading_day, load_config, setup_logging, validate_last_bar

PERIODS = ("m60", "daily")
PERIOD_LABELS = {"m60": "60分钟", "daily": "日线"}


def rho_for(cfg: dict, period: str) -> float:
    """买入方向的 rho=k2/k1；支持按周期覆盖。"""
    ind_cfg = cfg.get("indicator", {}) or {}
    overrides = ind_cfg.get("rho_by_period", {}) or {}
    return float(overrides.get(period, ind_cfg.get("rho", 1.0)))


def rho_sell_for(cfg: dict, period: str) -> float:
    """卖出方向阈值；高于买入阈值时形成死区，抑制小波动反复出卖点。"""
    ind_cfg = cfg.get("indicator", {}) or {}
    overrides = ind_cfg.get("rho_sell_by_period", {}) or {}
    return float(overrides.get(period, ind_cfg.get("rho_sell", rho_for(cfg, period))))


def signal_mode_for(cfg: dict, period: str) -> str:
    """信号口径；支持按周期覆盖（cross / state / ma7）。"""
    ind_cfg = cfg.get("indicator", {}) or {}
    overrides = ind_cfg.get("signal_mode_by_period", {}) or {}
    return str(overrides.get(period, ind_cfg.get("signal_mode", "state")))


def hold_threshold(cfg: dict, period: str) -> float:
    rule = cfg.get("indicator", {}).get("hold_rule", {}) or {}
    key = "turnover_threshold_60m" if period == "m60" else "turnover_threshold_daily"
    return float(rule.get(key, 1.0 if period == "m60" else 0.5))


def calculate(df, cfg: dict, period: str):
    ind_cfg = cfg.get("indicator", {}) or {}
    rule = ind_cfg.get("hold_rule", {}) or {}
    return compute_indicators(
        df,
        rho=rho_for(cfg, period),
        turnover_threshold=hold_threshold(cfg, period),
        require_bullish=bool(rule.get("require_bullish_candle", True)),
        signal_mode=signal_mode_for(cfg, period),
        rho_sell=rho_sell_for(cfg, period),
        ma7_lead_pct=float(ind_cfg.get("ma7_lead_pct", 0.0)) / 100.0,
        turnover_rel_min=float(ind_cfg.get("turnover_rel_min", 0.0)),
        signal_shift=int(ind_cfg.get("signal_shift", 0) or 0),
        skip_buy_after_sell=bool(ind_cfg.get("skip_buy_after_sell", False)),
    )


def daily_confirm_for(code: str, fetcher: MarketFetcher, cfg: dict,
                      logger: logging.Logger, cache: dict) -> str:
    """为 60 分钟消息取日线状态标签；同一标的只拉一次并缓存。"""
    if not cfg.get("periods", {}).get("use_as_confirm", False):
        return ""
    if code in cache:
        return cache[code]

    text = "日线：数据不可用"
    try:
        df, source = fetcher.fetch(code, "daily")
        ok, reason, clean = validate_last_bar(df, "daily")
        if not ok or clean.empty:
            logger.info("%s 日线确认跳过：%s", code, reason)
        else:
            sig = latest_signal(calculate(clean, cfg, "daily"))
            if sig:
                state = {"BUY": "B 买点", "SELL": "S 卖点"}.get(sig["signal"], "无交叉信号")
                hold = "持有" if sig["hold"] else "观望"
                text = f"日线二次确认：{state}（{hold}，{sig['time']}，来源 {source}）"
            else:
                text = "日线二次确认：数据不足"
    except Exception as exc:  # noqa: BLE001 - 确认标签失败不应阻断 60 分钟主流程
        logger.warning("%s 日线确认失败: %s", code, exc)
    cache[code] = text
    return text


def run_once(cfg: dict, logger: logging.Logger, period_filter: str | None = None,
             symbol_filter: str | None = None, force: bool = False) -> int:
    if not is_trading_day():
        logger.info("非交易日，跳过本次扫描")
        return 0

    symbols = [s for s in (cfg.get("symbols") or [])
               if isinstance(s, dict) and s.get("code")]
    if symbol_filter:
        symbols = [s for s in symbols if str(s["code"]) == str(symbol_filter)]
    if not symbols:
        logger.warning("配置中没有有效标的（symbols 为空），未执行扫描")
        return 1

    periods_cfg = cfg.get("periods", {}) or {}
    if period_filter:
        periods = [period_filter]
    else:
        periods = [p for p in PERIODS if (periods_cfg.get(p, {}) or {}).get("enabled", True)]

    fetcher = MarketFetcher(cfg, logger)
    notifier = Notifier(cfg, logger)
    store = StateStore(logger=logger)
    daily_cache: dict[str, str] = {}
    failures: list[str] = []
    pushed = 0

    for sym in symbols:
        code, name = str(sym["code"]), str(sym.get("name") or code)
        for period in periods:
            if period == "m60" and not (periods_cfg.get("m60", {}) or {}).get("push", True):
                continue
            if period == "daily" and not (periods_cfg.get("daily", {}) or {}).get("push", True):
                continue
            try:
                df, source = fetcher.fetch(code, period)
                ok, reason, clean = validate_last_bar(df, period)
                if not ok or clean.empty:
                    logger.info("%s %s 跳过：%s", code, period, reason)
                    continue

                sig = latest_signal(calculate(clean, cfg, period))
                if sig is None:
                    logger.info("%s %s: 有效 K 线不足，无法计算指标", code, period)
                    continue

                bar_time = sig["time"]
                if not force and not store.should_push(code, period, bar_time):
                    logger.debug("%s %s %s 已处理，跳过去重", code, period, bar_time)
                    continue

                if not sig["signal"]:
                    logger.info("%s %s %s 无买卖信号，状态：%s", code, period, bar_time,
                                sig["state"])
                    store.mark(code, period, bar_time, None)
                    continue

                payload = {
                    **sig,
                    "symbol": code,
                    "name": name,
                    "period": period,
                    "rho": rho_for(cfg, period),
                    "rho_sell": rho_sell_for(cfg, period),
                    "signal_mode": signal_mode_for(cfg, period),
                    "source": source,
                }
                if period == "m60":
                    payload["daily_confirm"] = daily_confirm_for(code, fetcher, cfg, logger, daily_cache)

                logger.info("%s %s 命中 %s @ %s 价格 %.2f", code, period,
                            sig["signal"], bar_time, sig["close"])
                if notifier.send_signal(payload):
                    store.mark(code, period, bar_time, sig["signal"])
                    pushed += 1
                else:
                    failures.append(f"{name}({code}) {PERIOD_LABELS[period]} 推送失败")
            except Exception as exc:  # noqa: BLE001 - 单标的失败不影响其他
                logger.exception("%s %s 处理失败: %s", code, period, exc)
                failures.append(f"{name}({code}) {PERIOD_LABELS[period]} 行情失败: {exc}")

    if failures:
        notifier.send_alert(failures)
    logger.info("扫描完成：推送 %d 条，失败 %d 项", pushed, len(failures))
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="波段助手 B/S 点监控")
    parser.add_argument("--config", help="配置文件路径，默认 wave-monitor/config.yaml")
    parser.add_argument("--once", action="store_true", help="执行一次扫描（默认行为）")
    parser.add_argument("--backtest", action="store_true", help="导出历史指标 CSV 供截图调参")
    parser.add_argument("--period", choices=PERIODS, help="仅运行指定周期")
    parser.add_argument("--symbol", help="仅运行指定代码（调试用）")
    parser.add_argument("--force", action="store_true", help="忽略去重状态，适合 dry-run 验证")
    parser.add_argument("--code", help="回测标的，默认取配置第一只")
    parser.add_argument("--bars", type=int, help="回测 K 线根数")
    parser.add_argument("--output", help="回测 CSV 输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    logger = setup_logging(cfg)
    logger.info("启动 wave-monitor: dry_run=%s, period=%s",
                cfg.get("general", {}).get("dry_run"), args.period or "all")

    if args.backtest:
        from backtest import run_backtest
        return run_backtest(cfg, args, logger)
    return run_once(cfg, logger, args.period, args.symbol, args.force)


if __name__ == "__main__":
    sys.exit(main())
