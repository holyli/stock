#!/usr/bin/env python3
"""批量预热 K 线缓存。

首次部署、或往 symbols 里新增标的时跑一次：逐只拉全量并落盘到
`data/kline_cache/`。之后日常扫描只走增量（每只只需十几行），
不会再把 westock 的配额打满。

脚本自带慢速节流与限频熔断：遇到「服务限频」立即停止，
已建的缓存保留，下次再跑会自动跳过已完成的标的。

用法：
  python3 warm_cache.py                # 预热 config 里全部标的、两个周期
  python3 warm_cache.py --periods m60  # 只预热 60 分钟
  python3 warm_cache.py --codes hk00700,300750
  python3 warm_cache.py --interval 8   # 每次请求间隔（秒），默认 6
"""

from __future__ import annotations

import argparse
import sys
import time

import yaml

from fetcher import MarketFetcher
from kline_cache import KlineCache
from utils import load_config, setup_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="批量预热 K 线缓存")
    ap.add_argument("--codes", help="逗号分隔的代码；默认取 config 全部标的")
    ap.add_argument("--periods", default="daily,m60", help="逗号分隔：daily,m60")
    ap.add_argument("--interval", type=float, default=6.0, help="请求间隔秒数")
    args = ap.parse_args(argv)

    cfg = load_config(None)
    logger = setup_logging(cfg)
    cache = KlineCache()

    symbols = [s for s in (cfg.get("symbols") or []) if isinstance(s, dict) and s.get("code")]
    if args.codes:
        want = {c.strip() for c in args.codes.split(",") if c.strip()}
        symbols = [s for s in symbols if str(s["code"]) in want]
    periods = [p.strip() for p in args.periods.split(",") if p.strip()]
    if not symbols:
        print("没有可预热的标的"); return 2

    # 用更慢的间隔，避免一次性建缓存又撞配额
    cfg.setdefault("fetch", {})["interval_seconds"] = args.interval
    cfg["fetch"]["fallback_to_akshare"] = False
    fetcher = MarketFetcher(cfg, logger)

    total = len(symbols) * len(periods)
    done = skipped = failed = 0
    print(f"预热 {len(symbols)} 只 × {len(periods)} 周期 = {total} 项，间隔 {args.interval}s\n")

    # 一次批量把缺缓存的标的全取回来：17 只 = 1 条命令，不再逐只发请求
    for period in periods:
        need = []
        for sym in symbols:
            code, name = str(sym["code"]), str(sym.get("name") or sym["code"])
            st = cache.stats(code, period)
            if st.get("cached", 0) >= 20:
                skipped += 1
                print(f"  ✓ {name}({code}) {period}: 已有 {st['cached']} 根，跳过")
            else:
                need.append((code, name))
        if not need:
            continue
        print(f"  → {period}: 批量取 {len(need)} 只（1 条命令）")
        data = fetcher.fetch_batch([c for c, _ in need], period, force=True)
        for code, name in need:
            df, source = data.get(code, (None, "skipped"))
            if df is None or df.empty:
                failed += 1
                print(f"\n✗ {name}({code}) {period}: 取数失败（{source}）")
                if source == "skipped":
                    print("  疑似被上游限频，先停止。已建好的缓存保留，"
                          "过一段时间再跑本脚本会自动跳过已完成的标的。")
                    _summary(done, skipped, failed, total)
                    return 1
                continue
            done += 1
            print(f"  ✓ {name}({code}) {period}: {len(df)} 根 "
                  f"（{str(df['time'].iloc[-1])[:16]}）")

    _summary(done, skipped, failed, total)
    return 0


def _summary(done: int, skipped: int, failed: int, total: int) -> None:
    print(f"\n新建 {done} 项 · 已存在 {skipped} 项 · 失败 {failed} 项 / 共 {total} 项")


if __name__ == "__main__":
    sys.exit(main())
