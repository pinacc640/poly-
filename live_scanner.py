#!/usr/bin/env python3
"""live_scanner.py — Polymarket 真实市场扫描器

从 Gamma API 拉取当前活跃市场，经过以下策略筛选和风控审批后输出交易机会：
  - stable_strategy()      稳健收敛（近到期 + 极端价格 + 高流动性）
  - volatility_strategy()  波动套利（大幅价格移动 fade）
  - smart_money_strategy() 聪明钱追踪（volume spike + 单边价格偏移）
  - arbitrage_strategy()   跨平台套利（Polymarket × Kalshi，--arbitrage）

可选 AI 增强：
  - --use-ai               用 DeepSeek-V3 重新估算每个市场的 true_prob

用法
----
    python live_scanner.py                    # 默认：拉取 200 条，标准配置
    python live_scanner.py --limit 500        # 更大样本
    python live_scanner.py --capital 200      # 调整账户资金
    python live_scanner.py --verbose          # 显示调试日志
    python live_scanner.py --dry-run          # 只拉数据，跳过策略（测试网络）
    python live_scanner.py --demo-edge        # 模拟 +15% 信息优势（演示用）
    python live_scanner.py --use-ai           # 用 AI oracle 替换 true_prob
    python live_scanner.py --arbitrage        # 启用 Polymarket × Kalshi 套利扫描

环境变量
--------
    SILICONFLOW_API_KEY   启用 --use-ai 所必须的 API Key

依赖
----
    pip install requests openai
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
    if not verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Live data source
# ---------------------------------------------------------------------------
class LiveDataSource:
    """Callable: GammaClient → [Market].

    Parameters
    ----------
    demo_edge :
        Add +0.15 to every market's true_prob to simulate a 15 % edge.
        USE FOR DEMONSTRATION ONLY.
    """

    def __init__(self, client: GammaClient, limit: int = 200, demo_edge: bool = False):
        self._client    = client
        self._limit     = limit
        self._demo_edge = demo_edge

    def __call__(self) -> List[Market]:
        log = logging.getLogger(__name__)
        log.info("Fetching up to %d markets from Gamma API …", self._limit)
        t0  = time.monotonic()
        raw = self._client.fetch_active_markets(limit=self._limit)
        log.info("API fetch complete: %d raw records in %.1fs",
                 len(raw), time.monotonic() - t0)

        markets = map_markets(raw)
        log.info("Mapped to %d Market objects (%d skipped)",
                 len(markets), len(raw) - len(markets))

        if self._demo_edge:
            for m in markets:
                m.true_prob = min(0.999, m.true_prob + 0.15)
            log.warning(
                "[DEMO-EDGE] true_prob boosted +15%% on all %d markets. "
                "NOT real alpha.", len(markets)
            )
        return markets


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="live_scanner",
        description="Polymarket live scanner — stable + vol + smart-money + arbitrage",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--limit", type=int, default=200, metavar="N",
                   help="Max markets to fetch from Gamma API")
    p.add_argument("--capital", type=float, default=50.0, metavar="USD",
                   help="Total account capital in USD")
    p.add_argument("--max-position", type=float, default=0.10, metavar="RATIO",
                   help="Max single position as fraction of capital")
    p.add_argument("--min-profit", type=float, default=0.10, metavar="USD",
                   help="Minimum expected profit per trade in USD")
    p.add_argument("--min-liquidity", type=float, default=100_000.0, metavar="USD",
                   help="Minimum market liquidity in USD")
    p.add_argument("--max-days", type=int, default=14, metavar="DAYS",
                   help="Max days to expiry for stable strategy")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch and map markets only — skip strategies")
    p.add_argument("--demo-edge", action="store_true",
                   help="Simulate +15%% info edge on true_prob. DEMO ONLY.")
    p.add_argument("--use-ai", action="store_true",
                   help=(
                       "Replace true_prob with AI-estimated probability via "
                       "DeepSeek-V3 (requires SILICONFLOW_API_KEY env var)"
                   ))
    p.add_argument("--arbitrage", action="store_true",
                   help=(
                       "Enable cross-platform arbitrage scan: "
                       "match Polymarket markets against Kalshi and flag "
                       "risk-free spreads (poly_yes + kalshi_no < 0.98)"
                   ))
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable debug logging")
    return p


# ---------------------------------------------------------------------------
# Dry-run summary
# ---------------------------------------------------------------------------
def _dry_run_summary(markets: List[Market]) -> None:
    from polymarket_scanner.config import DEFAULT_CONFIG as cfg
    print("\n" + "─" * 60)
    print("  DRY-RUN — market sample (no strategy/risk applied)")
    print("─" * 60)
    print(f"  Total markets mapped : {len(markets)}")
    near_expiry = [m for m in markets if m.days_to_expiry <= 14]
    high_price  = [m for m in markets if m.price >= 0.80 or m.price <= 0.20]
    liquid      = [m for m in markets if m.liquidity >= cfg.stable_min_liquidity]
    big_move    = [m for m in markets
                   if abs(m.price_change_24h) >= cfg.vol_min_abs_price_change_24h]
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

    # --- Config ---
    cfg = AccountConfig(
        total_capital             = args.capital,
        max_position_ratio        = args.max_position,
        min_absolute_profit       = args.min_profit,
        stable_min_liquidity      = args.min_liquidity,
        vol_min_liquidity         = args.min_liquidity,
        stable_max_days_to_expiry = args.max_days,
    )

    # --- Gamma health check ---
    client = GammaClient()
    log.info("Checking Gamma API connectivity …")
    if not client.health_check():
        log.error("Cannot reach Gamma API. Check network and try again.")
        sys.exit(1)
    log.info("Gamma API is reachable ✓")

    # --- AI Oracle pre-check ---
    if args.use_ai:
        import os
        # Support both DEEPSEEK_API_KEY (preferred) and legacy SILICONFLOW_API_KEY
        if not os.getenv("DEEPSEEK_API_KEY") and not os.getenv("SILICONFLOW_API_KEY"):
            log.error(
                "--use-ai requires DEEPSEEK_API_KEY (or SILICONFLOW_API_KEY) "
                "environment variable. Export it and try again."
            )
            sys.exit(1)
        log.info("AI Oracle enabled (DeepSeek + Brave Search) ✓")

    # --- Kalshi health check ---
    if args.arbitrage:
        from polymarket_scanner.kalshi_client import KalshiClient
        kalshi = KalshiClient()
        log.info("Checking Kalshi API connectivity …")
        if not kalshi.health_check():
            log.error("Cannot reach Kalshi API. Arbitrage scan disabled.")
            args.arbitrage = False
        else:
            log.info("Kalshi API is reachable ✓")

    data_source = LiveDataSource(
        client=client,
        limit=args.limit,
        demo_edge=args.demo_edge,
    )

    if args.dry_run:
        markets = data_source()
        _dry_run_summary(markets)
        return

    # --- Full scan ---
    flags = []
    if args.use_ai:    flags.append("AI-oracle")
    if args.arbitrage: flags.append("arbitrage")
    if args.demo_edge: flags.append("demo-edge +15%")
    flag_str = f" [{', '.join(flags)}]" if flags else ""

    log.info(
        "Running scanner (capital=$%.0f, max_pos=%.0f%%, min_profit=$%.2f)%s …",
        cfg.total_capital,
        cfg.max_position_ratio * 100,
        cfg.min_absolute_profit,
        flag_str,
    )

    scanner = MarketScanner(
        cfg           = cfg,
        data_source   = data_source,
        use_ai        = args.use_ai,
        run_arbitrage = args.arbitrage,
    )
    report: ScanReport = scanner.run()

    print(format_report(report))

    total_approved = (
        len(report.stable_approved)
        + len(report.volatility_approved)
        + len(report.smart_money_approved)
        + len(report.arbitrage_found)
    )
    if total_approved == 0:
        log.info("No approved opportunities found this scan.")

    # ── Telegram push (Phase 3) ──────────────────────────────────────────────
    # Reads TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from environment.
    # Silently skips if either variable is not set.
    from polymarket_scanner.notifier import TelegramNotifier
    notifier = TelegramNotifier()
    if notifier.is_enabled():
        tradeable = (
            len(report.stable_approved)
            + len(report.volatility_approved)
            + len(report.smart_money_approved)
        )
        if tradeable > 0:
            sent = notifier.send_report(report)
            log.info("📱 Telegram: sent %d message(s)", sent)
        else:
            log.info("📱 Telegram: no trade opportunities, skipping push")

    sys.exit(0)


if __name__ == "__main__":
    main()
