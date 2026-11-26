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
from scan_lock import ScanLock
from state_store import StateStore
import scan_log
from utils import (is_today_bar, load_config, market_of, setup_logging,
                   trading_day_status, turnover_multiple, validate_last_bar)

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


def run_once(cfg: dict, logger: logging.Logger, period_filter: str | None = None,
             symbol_filter: str | None = None, force: bool = False) -> int:
    status = trading_day_status()
    if not status["any"]:
        logger.info("%s：A 股与港股均休市，跳过本次扫描", status["date"])
        return 0
    logger.info("%s 交易日：A股=%s 港股=%s", status["date"],
                "开市" if status["cn"] else "休市",
                "未知" if status["hk"] is None else ("开市" if status["hk"] else "休市"))

    symbols = [s for s in (cfg.get("symbols") or [])
               if isinstance(s, dict) and s.get("code")]
    if symbol_filter:
        symbols = [s for s in symbols if str(s["code"]) == str(symbol_filter)]
    if not symbols:
        logger.warning("配置中没有有效标的（symbols 为空），未执行扫描")
        return 1

    lock = ScanLock(wait_seconds=(cfg.get("general", {}) or {}).get("lock_wait_seconds", 300),
                    logger=logger)
    if not lock.acquire():
        # 不写看板：这一轮没有真的扫，写进去会破坏「四类之和 = 标的数」的统计
        return 0
    try:
        return _scan(cfg, logger, symbols, period_filter, force)
    finally:
        lock.release()


def _scan(cfg: dict, logger: logging.Logger, symbols: list[dict],
          period_filter: str | None, force: bool) -> int:
    periods_cfg = cfg.get("periods", {}) or {}
    if period_filter:
        periods = [period_filter]
    else:
        periods = [p for p in PERIODS if (periods_cfg.get(p, {}) or {}).get("enabled", True)]
    periods = [p for p in periods
               if (periods_cfg.get(p, {}) or {}).get("push", True)]
    if not periods:
        logger.info("本周期没有启用推送的周期，跳过本次扫描")
        return 0
    # 两个周期彻底分离：一次只取本轮周期的数据，绝不跨周期取数
    # （60 分钟轮次不看日线，18:30 的日线轮次不看 60 分钟）
    logger.info("本轮扫描周期：%s（只取该周期行情）",
                "、".join(PERIOD_LABELS.get(p, p) for p in periods))

    fetcher = MarketFetcher(cfg, logger)
    notifier = Notifier(cfg, logger)
    if not notifier.ready:
        logger.warning("未读到企业微信 webhook key（%s），本轮所有推送都会失败；"
                       "请把 key 写入 ~/.env",
                       (cfg.get("notify", {}) or {}).get("webhook_key_env", "WECOM_WEBHOOK_KEY"))
    store = StateStore(logger=logger)
    failures: list[str] = []
    items: list[dict] = []   # 本次扫描逐项结果，供看板留档
    pushed = 0
    codes = [str(s["code"]) for s in symbols]

    for period in periods:
        # 同一周期一次批量取数：缓存已覆盖最新收线的标的直接不发请求
        batch = fetcher.fetch_batch(codes, period)
        for sym in symbols:
            code, name = str(sym["code"]), str(sym.get("name") or sym["code"])
            try:
                df, source = batch.get(code, (None, "skipped"))
                if source == "skipped":
                    # 本轮触发了上游限频且没有可用缓存，单独标记，便于看板统计
                    logger.info("%s %s 限频跳过（无可用缓存）", code, period)
                    items.append(dict(code=code, name=name, period=period,
                                      status="限频跳过"))
                    continue
                ok, reason, clean = validate_last_bar(df, period, market=market_of(code))
                if not ok or clean.empty:
                    logger.info("%s %s 跳过：%s", code, period, reason)
                    items.append(dict(code=code, name=name, period=period,
                                      status=f"跳过：{reason}"))
                    continue

                sig = latest_signal(calculate(clean, cfg, period))
                if sig is None:
                    logger.info("%s %s: 有效 K 线不足，无法计算指标", code, period)
                    items.append(dict(code=code, name=name, period=period,
                                      status="跳过：K 线不足"))
                    continue

                bar_time = sig["time"]
                if not force and not store.should_push(code, period, bar_time):
                    logger.debug("%s %s %s 已处理，跳过去重", code, period, bar_time)
                    continue

                if not sig["signal"]:
                    logger.info("%s %s %s 无买卖信号，状态：%s", code, period, bar_time,
                                sig["state"])
                    store.mark(code, period, bar_time, None)
                    items.append(dict(code=code, name=name, period=period, bar=bar_time,
                                      state=sig["state"], close=sig["close"],
                                      signal=None, status="无信号"))
                    continue

                # 只推当日信号：数据滞后或节假日残留的旧 K 线一律不推
                if not is_today_bar(bar_time):
                    logger.info("%s %s %s 信号日期不是今天，不推送", code, period, bar_time)
                    store.mark(code, period, bar_time, None)
                    items.append(dict(code=code, name=name, period=period, bar=bar_time,
                                      signal=None, state=sig["state"], close=sig["close"],
                                      status=f"非当日({str(bar_time)[:10]})，不推"))
                    continue

                payload = {
                    **sig,
                    "turnover_rel": turnover_multiple(clean),
                    "symbol": code,
                    "name": name,
                    "period": period,
                    "rho": rho_for(cfg, period),
                    "rho_sell": rho_sell_for(cfg, period),
                    "signal_mode": signal_mode_for(cfg, period),
                    "source": source,
                }

                logger.info("%s %s 命中 %s @ %s 价格 %.2f", code, period,
                            sig["signal"], bar_time, sig["close"])
                if notifier.send_signal(payload):
                    store.mark(code, period, bar_time, sig["signal"])
                    pushed += 1
                    items.append(dict(code=code, name=name, period=period, bar=bar_time,
                                      signal=sig["signal"], state=sig["state"],
                                      close=sig["close"], status="已推送"))
                else:
                    failures.append(f"{name}({code}) {PERIOD_LABELS[period]} 推送失败")
                    items.append(dict(code=code, name=name, period=period, bar=bar_time,
                                      signal=sig["signal"], state=sig["state"],
                                      close=sig["close"], status="失败：推送失败"))
            except Exception as exc:  # noqa: BLE001 - 单标的失败不影响其他
                logger.exception("%s %s 处理失败: %s", code, period, exc)
                failures.append(f"{name}({code}) {PERIOD_LABELS[period]} 行情失败: {exc}")
                items.append(dict(code=code, name=name, period=period,
                                  status=f"失败：{exc}"))

    if failures:
        notifier.send_alert(failures)
    logger.info("扫描完成：推送 %d 条，失败 %d 项", pushed, len(failures))
    scan_log.record_run(items, pushed, failures, period_filter)
    logger.info("本次结果已记入看板：output/扫描看板.html")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="波段助手 B/S 点监控")
    parser.add_argument("--config", help="配置文件路径，默认 wave-monitor/config.yaml")
    parser.add_argument("--once", action="store_true", help="执行一次扫描（默认行为）")
    parser.add_argument("--backtest", action="store_true", help="导出历史指标 CSV 供截图调参")
    parser.add_argument("--period", choices=PERIODS, help="仅运行指定周期")
    parser.add_argument("--symbol", help="仅运行指定代码（调试用）")
    parser.add_argument("--force", action="store_true", help="忽略去重状态，适合 dry-run 验证")
    parser.add_argument("--live", action="store_true",
                        help="本次临时关闭 dry_run，真实推送（用于验证企微 webhook，不写回配置）")
    parser.add_argument("--code", help="回测标的，默认取配置第一只")
    parser.add_argument("--bars", type=int, help="回测 K 线根数")
    parser.add_argument("--output", help="回测 CSV 输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.live:
        cfg.setdefault("general", {})["dry_run"] = False
    logger = setup_logging(cfg)
    if args.live:
        logger.warning("--live 已启用：本次将真实推送到企业微信（不修改 config.yaml）")
    logger.info("启动 wave-monitor: dry_run=%s, period=%s",
                cfg.get("general", {}).get("dry_run"), args.period or "all")

    if args.backtest:
        from backtest import run_backtest
        return run_backtest(cfg, args, logger)
    return run_once(cfg, logger, args.period, args.symbol, args.force)


if __name__ == "__main__":
    sys.exit(main())
