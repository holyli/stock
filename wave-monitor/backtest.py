"""历史回测与信号导出：用同花顺截图对照 rho，不推送任何消息。

    python3 main.py --backtest --code 300750 --period daily
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path

from fetcher import MarketFetcher
from indicator import EXPORT_COLUMNS, compute_indicators
from utils import PROJECT_DIR, validate_last_bar


def run_backtest(cfg: dict, args, logger: logging.Logger) -> int:
    symbols = [s for s in (cfg.get("symbols") or []) if isinstance(s, dict) and s.get("code")]
    code = str(args.code or (symbols[0]["code"] if symbols else ""))
    if not code:
        logger.error("请通过 --code 指定标的，或在 config.yaml 的 symbols 中填入标的")
        return 1

    period = args.period or "m60"
    bars = int(args.bars or cfg.get("fetch", {}).get("history_bars", 120))
    cfg_run = copy.deepcopy(cfg)
    cfg_run.setdefault("fetch", {})["history_bars"] = bars

    fetcher = MarketFetcher(cfg_run, logger)
    df, source = fetcher.fetch(code, period)
    ok, reason, clean = validate_last_bar(df, period)
    if ok and not clean.empty:
        data = clean
    else:
        data = df  # 全都不闭合时仍导出，便于人工核对
        logger.warning("无已闭合 K 线（%s），导出原始行情供排查", reason)

    ind_cfg = cfg.get("indicator", {}) or {}
    rule = ind_cfg.get("hold_rule", {}) or {}
    overrides = ind_cfg.get("rho_by_period", {}) or {}
    rho = float(overrides.get(period, ind_cfg.get("rho", 1.0)))
    threshold_key = "turnover_threshold_60m" if period == "m60" else "turnover_threshold_daily"
    mode = str((ind_cfg.get("signal_mode_by_period", {}) or {}).get(
        period, ind_cfg.get("signal_mode", "state")))
    result = compute_indicators(
        data,
        rho=rho,
        turnover_threshold=float(rule.get(threshold_key, 1.0 if period == "m60" else 0.5)),
        require_bullish=bool(rule.get("require_bullish_candle", True)),
        signal_mode=mode,
        rho_sell=float((ind_cfg.get("rho_sell_by_period", {}) or {}).get(
            period, ind_cfg.get("rho_sell", rho))),
        ma7_lead_pct=float(ind_cfg.get("ma7_lead_pct", 0.0)) / 100.0,
        turnover_rel_min=float(ind_cfg.get("turnover_rel_min", 0.0)),
        signal_shift=int(ind_cfg.get("signal_shift", 0) or 0),
        skip_buy_after_sell=bool(ind_cfg.get("skip_buy_after_sell", False)),
    )

    out_path = Path(args.output) if args.output else (
        PROJECT_DIR / "data" / f"backtest_{code}_{period}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result[list(EXPORT_COLUMNS)].to_csv(out_path, index=False, float_format="%.4f", encoding="utf-8-sig")

    signals = result[result["buy"] | result["sell"]]
    rho_sell = float((ind_cfg.get("rho_sell_by_period", {}) or {}).get(
        period, ind_cfg.get("rho_sell", rho)))
    print(f"标的 {code} 周期 {period} 来源 {source} 共 {len(result)} 根 K 线，"
          f"rho={rho}/rho_sell={rho_sell}，signal_mode={mode}")
    print(f"CSV: {out_path}")
    print("最近 20 个复刻买卖点（时间, 信号, 收盘价, 状态）：")
    for _, row in signals.tail(20).iterrows():
        label = "B" if bool(row["buy"]) else "S"
        print(f"  {row['time']:%Y-%m-%d %H:%M}  {label}  {row['close']:.2f}  {row['state']}")
    logger.info("回测完成: %s %s, %d 根 K 线, %d 个买卖点, rho=%s",
                code, period, len(result), len(signals), rho)
    return 0
