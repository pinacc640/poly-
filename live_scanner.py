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
    DEEPSEEK_API_KEY   启用 --use-ai 所必须的 API Key
    BRAVE_API_KEY      Brave Search API Key（可选，增强 AI 搜索）
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  Telegram 推送（可选）
    POLY_WALLET_ADDRESS  持仓查询（可选）

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
# .env 加载（不依赖 python-dotenv）
# ---------------------------------------------------------------------------
def _load_dotenv_early() -> None:
    """在程序最开始加载 .env 文件，确保所有环境变量可用。"""
    import os
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, _, v = line.partition("=")
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass
            break

# 立即执行
_load_dotenv_early()


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
    p.add_argument("--limit", type=int, default=1000, metavar="N",
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
                       "DeepSeek-V3 (requires DEEPSEEK_API_KEY env var)"
                   ))
    p.add_argument("--arbitrage", action="store_true",
                   help=(
                       "Enable cross-platform arbitrage scan: "
                       "match Polymarket markets against Kalshi and flag "
                       "risk-free spreads (poly_yes + kalshi_no < 0.98)"
                   ))
    p.add_argument("--positions", action="store_true",
                   help="Scan only your portfolio positions (real-time from Polymarket)")
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

    # ── Phase 4: on-chain portfolio sync ────────────────────────────────────
    # Syncs wallet positions to portfolio.json BEFORE the scan so we can
    # split markets into "fresh" (no position) vs "held" (already in).
    portfolio = {}
    held_market_ids = set()
    
    # 只有在非 positions 模式下才做持仓同步
    if not args.positions:
        try:
            from polymarket_scanner.portfolio_sync import sync_portfolio, load_portfolio
            try:
                portfolio = sync_portfolio()
                log.info("📊 Portfolio sync: %d active position(s)", len(portfolio))
            except Exception as _sync_exc:
                log.warning("Portfolio sync failed (%s) — using local cache if available", _sync_exc)
                portfolio = load_portfolio()
            held_market_ids = set(portfolio.keys())
        except ImportError:
            log.debug("portfolio_sync not available, skipping")

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
        try:
            from polymarket_scanner.kalshi_client import KalshiClient
            kalshi = KalshiClient()
            log.info("Checking Kalshi API connectivity …")
            if not kalshi.health_check():
                log.error("Cannot reach Kalshi API. Arbitrage scan disabled.")
                args.arbitrage = False
            else:
                log.info("Kalshi API is reachable ✓")
        except ImportError:
            log.warning("kalshi_client not available. Arbitrage disabled.")
            args.arbitrage = False

    # --- 数据源选择 ---
    if args.positions:
        # 持仓模式：只扫描你的持仓
        try:
            from polymarket_scanner.positions import fetch_positions, AuthError, PositionFetchError
            log.info("📌 --positions 模式：实时拉取 CLOB 账户持仓…")
            try:
                markets = fetch_positions(timeout=15, logger=log)
            except AuthError as e:
                print(f"\n[认证错误] {e}\n")
                sys.exit(1)
            except PositionFetchError as e:
                print(f"\n[API 错误] {e}\n")
                sys.exit(1)

            if not markets:
                print("ℹ️  当前账户无持仓，无需扫描。")
                sys.exit(0)

            log.info("✅ CLOB 实时持仓：%d 个仓位", len(markets))
        except ImportError:
            log.error("positions module not available. Use --positions without positions.py")
            sys.exit(1)
    else:
        # 全市场模式
        data_source = LiveDataSource(
            client=client,
            limit=args.limit,
            demo_edge=args.demo_edge,
        )

        if args.dry_run:
            markets = data_source()
            _dry_run_summary(markets)
            return

        all_markets   = data_source()
        fresh_markets = [m for m in all_markets if m.market_id not in held_market_ids]
        held_markets  = [m for m in all_markets if m.market_id     in held_market_ids]
        
        log.info(
            "Market split: %d fresh (will scan) | %d held (will monitor)",
            len(fresh_markets), len(held_markets),
        )
        markets = fresh_markets

    # --- Flags ---
    flags = []
    if args.use_ai:    flags.append("AI-oracle")
    if args.arbitrage: flags.append("arbitrage")
    if args.demo_edge: flags.append("demo-edge +15%")
    if args.positions: flags.append("positions-only")
    flag_str = f" [{', '.join(flags)}]" if flags else ""

    log.info(
        "Running scanner (capital=$%.0f, max_pos=%.0f%%, min_profit=$%.2f)%s …",
        cfg.total_capital,
        cfg.max_position_ratio * 100,
        cfg.min_absolute_profit,
        flag_str,
    )

    # --- AI Oracle 增强（可选）---
    use_ai = args.use_ai
    
    if use_ai:
        try:
            from polymarket_scanner.ai_oracle import AIOracle
            print("🔮 AI Oracle 模式 — 正在用 DeepSeek + Brave Search 评估概率…\n")
            
            # 智能过滤：只对高流动性市场调用 AI
            # 流动性 >= $100K 的市场才用 AI，其他跳过
            ai_threshold = 100_000
            ai_markets = [m for m in markets if m.liquidity >= ai_threshold]
            skip_markets = [m for m in markets if m.liquidity < ai_threshold]
            
            log.info("AI 前置过滤：%d 个市场符合条件（流动性>=%s）且价格区间/波动），%d 个跳过 AI 直接用原始概率",
                     len(ai_markets), f"${ai_threshold:,}", len(skip_markets))
            
            if ai_markets:
                oracle = AIOracle(fallback_on_error=True, timeout=15)
                ai_markets = oracle.enrich_all(ai_markets)
                log.info("AI Oracle 增强完成（实际调用 %d 次，节省 %d 次）", len(ai_markets), len(skip_markets))
            
            # 合并
            markets = ai_markets + skip_markets
        except ImportError:
            log.warning("ai_oracle not available, skipping AI enhancement")

    # --- 运行扫描器 ---
    scanner = MarketScanner(
        cfg           = cfg,
        data_source   = lambda: markets,
        use_ai        = use_ai,
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

    # ── Telegram push (with dedup + fundamentals check) ─────────────────────
    try:
        from polymarket_scanner.notifier import TelegramNotifier
        from polymarket_scanner.dedup import PushDedup
        from polymarket_scanner.fundamentals import FundamentalsChecker

        notifier = TelegramNotifier()
        if notifier.is_enabled():
            # Collect all approved opportunities
            all_approved = (
                report.stable_approved
                + report.volatility_approved
                + report.smart_money_approved
            )

            if all_approved:
                # Step A: Dedup — filter out markets already pushed in the last 24h
                dedup = PushDedup()
                new_opps = dedup.filter_new(all_approved)
                log.info("📋 Dedup: %d total → %d new (skipped %d already pushed)",
                         len(all_approved), len(new_opps), len(all_approved) - len(new_opps))

                if new_opps:
                    # Step B: Fundamentals check — DeepSeek sanity filter on new opps
                    checker = FundamentalsChecker()
                    if checker.is_available():
                        verified_opps = checker.check_opportunities(new_opps)
                        log.info("🔍 Fundamentals: %d/%d passed DeepSeek sanity check",
                                 len(verified_opps), len(new_opps))
                    else:
                        verified_opps = new_opps
                        log.debug("Fundamentals check skipped (DEEPSEEK_API_KEY not set)")

                    # Step C: Send Telegram alerts for verified opportunities
                    if verified_opps:
                        # Rebuild a mini-report with only the verified opps for the notifier
                        push_report = ScanReport(
                            stable_approved=[x for x in verified_opps if x in report.stable_approved],
                            volatility_approved=[x for x in verified_opps if x in report.volatility_approved],
                            smart_money_approved=[x for x in verified_opps if x in report.smart_money_approved],
                            total_markets_scanned=report.total_markets_scanned,
                            config=report.config,
                            ai_oracle_used=report.ai_oracle_used,
                        )
                        sent = notifier.send_report(push_report)
                        log.info("📱 Telegram: sent %d message(s)", sent)

                        # Step D: Mark pushed so next run won't re-alert
                        dedup.mark_pushed(verified_opps)
                    else:
                        log.info("📱 Telegram: all new opps failed fundamentals check, no push")
                else:
                    log.info("📱 Telegram: all opportunities already pushed within 24h, skipping")
            else:
                log.info("📱 Telegram: no approved trade opportunities, skipping push")
    except ImportError as e:
        log.debug("Telegram modules not available: %s", e)

    # ── Phase 4: position risk monitor ───────────────────────────────────────
    if portfolio and not args.positions:
        try:
            from polymarket_scanner.position_monitor import PositionMonitor
            monitor    = PositionMonitor()
            pos_alerts = monitor.check(portfolio, markets)   # use ALL markets for price lookup

            if pos_alerts:
                log.info("📊 Position monitor: %d alert(s) triggered", len(pos_alerts))
                try:
                    notifier = TelegramNotifier()
                    if notifier.is_enabled():
                        sent = sum(1 for msg in pos_alerts if notifier._post(msg))
                        log.info("📱 Telegram (position alerts): sent %d/%d", sent, len(pos_alerts))
                    else:
                        raise ImportError("notifier not enabled")
                except ImportError:
                    # No Telegram configured — print to terminal
                    import re
                    print("\n" + "═" * 65)
                    print("  📊 POSITION RISK ALERTS")
                    print("═" * 65)
                    for msg in pos_alerts:
                        print(re.sub(r"<[^>]+>", "", msg))   # strip HTML tags
                        print()
            else:
                log.info("📊 Position monitor: all %d position(s) within normal range", len(portfolio))
        except ImportError:
            log.debug("position_monitor not available")
    else:
        log.debug("Position monitor skipped — no active positions or positions mode")

    sys.exit(0)


if __name__ == "__main__":
    main()