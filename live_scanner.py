#!/usr/bin/env python3
"""live_scanner.py — Polymarket 真实市场扫描器

从 Gamma API 拉取当前活跃市场，经过策略筛选和风控审批后输出交易机会。

用法
----
    python live_scanner.py                  # 默认：拉取 200 条，标准配置
    python live_scanner.py --limit 500      # 更大样本
    python live_scanner.py --capital 200    # 调整账户资金
    python live_scanner.py --verbose        # 显示调试日志
    python live_scanner.py --dry-run        # 只拉数据，跳过策略（测试网络）

依赖
----
    pip install requests
"""

import argparse
import logging
import sys
import time
from typing import List

from polymarket_scanner.config import AccountConfig
from polymarket_scanner.formatter import format_report
from polymarket_scanner.gamma_client import GammaClient
from polymarket_scanner.mapper import map_markets
from polymarket_scanner.models import Market
from polymarket_scanner.scanner import MarketScanner, ScanReport


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=level,
        stream=sys.stderr,
    )
    # Suppress noisy urllib3 debug output unless explicitly verbose
    if not verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Live data source — plugs into MarketScanner.data_source
# ---------------------------------------------------------------------------
class LiveDataSource:
    """Callable that GammaClient → [Market].

    Designed to be passed as the ``data_source`` argument to
    MarketScanner so the rest of the pipeline is unchanged.
    """

    def __init__(self, client: GammaClient, limit: int = 200):
        self._client = client
        self._limit  = limit

    def __call__(self) -> List[Market]:
        log = logging.getLogger(__name__)
        log.info("Fetching up to %d markets from Gamma API …", self._limit)
        t0 = time.monotonic()

        raw = self._client.fetch_active_markets(limit=self._limit)

        elapsed = time.monotonic() - t0
        log.info("API fetch complete: %d raw records in %.1fs", len(raw), elapsed)

        markets = map_markets(raw)
        log.info("Mapped to %d valid Market objects (%d skipped)",
                 len(markets), len(raw) - len(markets))
        return markets


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="live_scanner",
        description="Polymarket live market scanner — 80% stable + 20% volatility",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--limit", type=int, default=200, metavar="N",
        help="Max number of markets to fetch from Gamma API",
    )
    p.add_argument(
        "--capital", type=float, default=50.0, metavar="USD",
        help="Total account capital in USD",
    )
    p.add_argument(
        "--max-position", type=float, default=0.10, metavar="RATIO",
        help="Max single position as fraction of capital (e.g. 0.10 = 10%%)",
    )
    p.add_argument(
        "--min-profit", type=float, default=0.50, metavar="USD",
        help="Minimum expected profit per trade in USD",
    )
    p.add_argument(
        "--min-liquidity", type=float, default=100_000.0, metavar="USD",
        help="Minimum market liquidity in USD",
    )
    p.add_argument(
        "--max-days", type=int, default=14, metavar="DAYS",
        help="Max days to expiry for stable strategy",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and map markets only — skip strategy/risk (network test)",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    return p


# ---------------------------------------------------------------------------
# Dry-run report
# ---------------------------------------------------------------------------
def _dry_run_summary(markets: List[Market]) -> None:
    """Print a quick summary table without running any strategy."""
    from polymarket_scanner.config import DEFAULT_CONFIG as cfg

    print("\n" + "─" * 60)
    print("  DRY-RUN — market sample (no strategy/risk applied)")
    print("─" * 60)
    print(f"  Total markets mapped : {len(markets)}")

    # Quick stats
    near_expiry  = [m for m in markets if m.days_to_expiry <= 14]
    high_price   = [m for m in markets if m.price >= 0.80 or m.price <= 0.20]
    liquid       = [m for m in markets if m.liquidity >= cfg.stable_min_liquidity]
    big_move     = [m for m in markets if abs(m.price_change_24h) >= cfg.vol_min_abs_price_change_24h]

    print(f"  Expiry <= 14 days    : {len(near_expiry)}")
    print(f"  Extreme price        : {len(high_price)}")
    print(f"  Liquid (>= $100K)   : {len(liquid)}")
    print(f"  24h move >= 5%       : {len(big_move)}")
    print()
    print("  First 5 markets:")
    for m in markets[:5]:
        print(f"    [{m.market_id}] {m.question[:60]}")
        print(f"           price={m.price:.3f}  liq=${m.liquidity:,.0f}"
              f"  Δ24h={m.price_change_24h:+.2%}  days={m.days_to_expiry}"
              f"  cat={m.category}")
    print("─" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = _build_parser()
    args   = parser.parse_args()

    _setup_logging(args.verbose)
    log = logging.getLogger(__name__)

    # --- Build config from CLI args ---
    cfg = AccountConfig(
        total_capital        = args.capital,
        max_position_ratio   = args.max_position,
        min_absolute_profit  = args.min_profit,
        stable_min_liquidity = args.min_liquidity,
        vol_min_liquidity    = args.min_liquidity,
        stable_max_days_to_expiry = args.max_days,
    )

    # --- Health check ---
    client = GammaClient()
    log.info("Checking Gamma API connectivity …")
    if not client.health_check():
        log.error("Cannot reach Gamma API. Check network and try again.")
        sys.exit(1)
    log.info("Gamma API is reachable ✓")

    # --- Fetch + map ---
    data_source = LiveDataSource(client=client, limit=args.limit)

    if args.dry_run:
        markets = data_source()
        _dry_run_summary(markets)
        return

    # --- Full scan ---
    scanner = MarketScanner(cfg=cfg, data_source=data_source)
    log.info("Running scanner (capital=$%.0f, max_pos=%.0f%%, min_profit=$%.2f) …",
             cfg.total_capital, cfg.max_position_ratio * 100, cfg.min_absolute_profit)

    report: ScanReport = scanner.run()

    print(format_report(report))

    # Exit with non-zero if no approved opportunities (useful in cron/CI)
    total_approved = len(report.stable_approved) + len(report.volatility_approved)
    if total_approved == 0:
        log.info("No approved opportunities found.")
        sys.exit(0)   # still exit 0 — not an error, just no trades today


if __name__ == "__main__":
    main()
