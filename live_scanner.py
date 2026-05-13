#!/usr/bin/env python3
"""live_scanner.py — 生产级 CLI 入口。

Phase 4 新增：
  - 启动时自动同步链上持仓 (portfolio_sync)
  - 扫描后对已持仓市场做止盈/止损/加仓风控 (position_monitor)
  - 新机会 vs 持仓分开推送 Telegram

用法示例
--------
    python live_scanner.py --capital 70
    python live_scanner.py --use-ai --capital 70
    python live_scanner.py --mock
    python live_scanner.py --verbose

环境变量
--------
    DEEPSEEK_API_KEY        AI Oracle（可选）
    BRAVE_API_KEY           联网搜索（可选）
    TELEGRAM_BOT_TOKEN      Telegram 推送
    TELEGRAM_CHAT_ID        Telegram 目标频道
    POLY_WALLET_ADDRESS     Polymarket Proxy 地址（默认硬编码）
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

from polymarket_scanner.config import AccountConfig
from polymarket_scanner.formatter import format_report
from polymarket_scanner.mock_data import load_mock_markets
from polymarket_scanner.models import Market
from polymarket_scanner.scanner import MarketScanner

# ---------------------------------------------------------------------------
# Gamma API 常量
# ---------------------------------------------------------------------------
GAMMA_BASE_URL    = "https://gamma-api.polymarket.com"
GAMMA_MARKETS_URL = f"{GAMMA_BASE_URL}/markets"
GAMMA_TIMEOUT     = 10


# ---------------------------------------------------------------------------
# Gamma API helpers
# ---------------------------------------------------------------------------

def _gamma_fetch_raw(limit: int, timeout: int) -> List[dict]:
    params = urllib.parse.urlencode({
        "active": "true",
        "closed": "false",
        "limit":  min(limit, 500),
    })
    url = f"{GAMMA_MARKETS_URL}?{params}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "polymarket-scanner/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Gamma API HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Gamma API 连接失败: {e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"Gamma API 未知错误: {e}") from e


def _parse_gamma_market(raw: dict) -> Optional[Market]:
    try:
        market_id = str(raw.get("id") or raw.get("conditionId") or "").strip()
        question  = str(raw.get("question") or raw.get("title") or "").strip()
        if not market_id or not question:
            return None

        outcomes = raw.get("outcomePrices") or []
        if isinstance(outcomes, list) and len(outcomes) >= 1:
            price = float(outcomes[0])
        else:
            price = float(raw.get("lastTradePrice") or 0.5)
        price = max(0.0, min(1.0, price))

        liquidity        = float(raw.get("liquidity")      or 0)
        volume_24h       = float(raw.get("volume24hr")     or 0)
        volume_prev_24h  = float(raw.get("volume1wk")      or 0) / 7
        price_change_24h = float(raw.get("priceChange24h") or 0)

        end_date_str = raw.get("endDate") or raw.get("endDateIso") or ""
        days_to_expiry = 30
        if end_date_str:
            try:
                end_dt = datetime.datetime.fromisoformat(
                    end_date_str.replace("Z", "+00:00")
                )
                now = datetime.datetime.now(datetime.timezone.utc)
                days_to_expiry = max(0, (end_dt - now).days)
            except Exception:
                pass

        tags = raw.get("tags") or []
        if isinstance(tags, list) and tags:
            category = (tags[0].get("label") if isinstance(tags[0], dict)
                        else str(tags[0])).lower()
        else:
            category = str(raw.get("category") or "general").lower()

        return Market(
            market_id        = market_id,
            question         = question,
            category         = category,
            price            = price,
            liquidity        = liquidity,
            volume_24h       = volume_24h,
            volume_prev_24h  = volume_prev_24h,
            price_change_24h = price_change_24h,
            days_to_expiry   = days_to_expiry,
            true_prob        = price,
        )
    except Exception:
        return None


def fetch_live_markets(
    limit: int,
    min_liquidity: float,
    timeout: int,
    logger: logging.Logger,
) -> Optional[List[Market]]:
    logger.info("正在连接 Gamma API，拉取最多 %d 个市场…", limit)
    try:
        raw_list = _gamma_fetch_raw(limit, timeout=min(timeout, GAMMA_TIMEOUT))
    except RuntimeError as e:
        logger.warning("Gamma API 不可用 (%s)，降级为 mock 数据。", e)
        return None

    markets: List[Market] = []
    for raw in raw_list:
        m = _parse_gamma_market(raw)
        if m and m.liquidity >= min_liquidity:
            markets.append(m)

    if not markets:
        logger.warning("Gamma API 返回 0 条有效市场，降级为 mock 数据。")
        return None

    logger.info("✅ Gamma API: %d 条市场（流动性 >= $%.0f）", len(markets), min_liquidity)
    return markets


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="live_scanner",
        description="Polymarket Market Scanner — Phase 4（Portfolio Sync + Position Monitor）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--capital",       type=float, default=50.0,   metavar="USD")
    p.add_argument("--use-ai",        action="store_true", default=False)
    p.add_argument("--mock",          action="store_true", default=False)
    p.add_argument("--limit",         type=int,   default=1000,   metavar="N")
    p.add_argument("--min-liquidity", type=float, default=50_000, metavar="USD",
                   dest="min_liquidity")
    p.add_argument("--timeout",       type=int,   default=15,     metavar="SEC")
    p.add_argument("--verbose", "-v", action="store_true", default=False)

    ai = p.add_argument_group("AI Oracle 参数（仅 --use-ai 时生效）")
    ai.add_argument("--model",       type=str,   default="deepseek-chat")
    ai.add_argument("--max-results", type=int,   default=5, dest="max_results")
    ai.add_argument("--temperature", type=float, default=0.2)
    ai.add_argument("--max-tokens",  type=int,   default=256, dest="max_tokens")
    ai.add_argument("--no-fallback", action="store_true", default=False)
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args  = _build_parser().parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger(__name__)

    # ── 1. 账户配置 ──────────────────────────────────────────────────────────
    cfg = AccountConfig(total_capital=args.capital)
    logger.info("账户资金: $%.2f", args.capital)

    # ── 2. 【Phase 4】链上持仓同步 ────────────────────────────────────────────
    # 先同步链上持仓到 portfolio.json，后续扫描时区分「新机会」vs「已持仓」
    from polymarket_scanner.portfolio_sync import sync_portfolio, load_portfolio
    try:
        portfolio = sync_portfolio()
        logger.info("📊 Portfolio: %d 个活跃持仓已同步", len(portfolio))
    except Exception as exc:
        logger.warning("持仓同步失败 (%s)，将使用本地缓存（如有）", exc)
        portfolio = load_portfolio()
    held_market_ids = set(portfolio.keys())

    # ── 3. 拉取市场数据 ───────────────────────────────────────────────────────
    using_mock = False

    if args.mock:
        logger.info("--mock 模式")
        markets    = load_mock_markets()
        using_mock = True
    else:
        markets = fetch_live_markets(
            limit=args.limit,
            min_liquidity=args.min_liquidity,
            timeout=args.timeout,
            logger=logger,
        )
        if markets is None:
            logger.info("降级为 mock 数据。")
            markets    = load_mock_markets()
            using_mock = True

    src_label = "⚠️  mock 数据" if using_mock else "✅ Gamma API 实时数据"
    logger.info("数据来源：%s，共 %d 个市场", src_label, len(markets))

    # ── 4. 分类：未持仓 vs 已持仓 ─────────────────────────────────────────────
    fresh_markets = [m for m in markets if m.market_id not in held_market_ids]
    held_markets  = [m for m in markets if m.market_id in held_market_ids]
    logger.info(
        "市场分类：%d 个未持仓（走 EV/Kelly 扫描），%d 个已持仓（走风控监控）",
        len(fresh_markets), len(held_markets),
    )

    # ── 5. AI Oracle 增强（可选，仅对未持仓市场）────────────────────────────────
    if args.use_ai:
        from polymarket_scanner.ai_oracle import AIOracle
        print("🔮 AI Oracle 模式 — DeepSeek + Brave Search…\n")
        try:
            oracle = AIOracle(
                fallback_on_error = not args.no_fallback,
                timeout           = args.timeout,
                model             = args.model,
                max_results       = args.max_results,
                temperature       = args.temperature,
                max_tokens        = args.max_tokens,
            )
            fresh_markets = oracle.enrich_all(fresh_markets)
            logger.info("AI Oracle 增强完成（%d 个未持仓市场）", len(fresh_markets))
        except ValueError as exc:
            logger.error("[ERROR] %s", exc)
            sys.exit(1)

    # ── 6. EV/Kelly 扫描（仅未持仓市场）─────────────────────────────────────────
    scanner = MarketScanner(cfg=cfg, data_source=lambda: fresh_markets)
    report  = scanner.run()

    if using_mock:
        print("⚠️  注意：当前结果基于 mock 数据\n")
    print(format_report(report))

    total_new = (
        len(report.stable_approved)
        + len(report.volatility_approved)
        + len(getattr(report, "smart_money_approved", []))
    )
    if total_new == 0:
        logger.info("本次扫描未发现新机会。")

    # ── 7. Telegram 推送：新机会 ───────────────────────────────────────────────
    try:
        from polymarket_scanner.notifier import TelegramNotifier
        notifier = TelegramNotifier()
    except ImportError:
        notifier = None

    if notifier and notifier.is_enabled() and total_new > 0:
        try:
            from polymarket_scanner.dedup import PushDedup
            from polymarket_scanner.fundamentals import FundamentalsChecker
            dedup    = PushDedup()
            checker  = FundamentalsChecker()
            all_new  = (
                report.stable_approved
                + report.volatility_approved
                + getattr(report, "smart_money_approved", [])
            )
            new_opps      = dedup.filter_new(all_new)
            verified_opps = checker.check_opportunities(new_opps)
            if verified_opps:
                from polymarket_scanner.scanner import ScanReport
                push_report = ScanReport(
                    stable_approved      = [x for x in verified_opps if x in report.stable_approved],
                    volatility_approved  = [x for x in verified_opps if x in report.volatility_approved],
                    smart_money_approved = [x for x in verified_opps
                                            if x in getattr(report, "smart_money_approved", [])],
                    total_markets_scanned= report.total_markets_scanned,
                    config               = report.config,
                )
                sent = notifier.send_report(push_report)
                logger.info("📱 Telegram（新机会）: 发送 %d 条", sent)
                dedup.mark_pushed(verified_opps)
        except ImportError:
            # dedup / fundamentals 模块不存在时直接发
            sent = notifier.send_report(report)
            logger.info("📱 Telegram（新机会）: 发送 %d 条", sent)

    # ── 8. 【Phase 4】持仓风控监控 ────────────────────────────────────────────
    # 对「已持仓市场」做止盈 / 止损 / 加仓判断，并发专属警报
    if portfolio:
        from polymarket_scanner.position_monitor import PositionMonitor
        monitor      = PositionMonitor()
        # 用全量市场数据（含已持仓）做价格比对
        pos_alerts   = monitor.check(portfolio, markets)

        if pos_alerts:
            logger.info("📊 持仓风控：%d 条警报触发", len(pos_alerts))
            if notifier and notifier.is_enabled():
                sent = 0
                for alert_msg in pos_alerts:
                    if notifier._post(alert_msg):
                        sent += 1
                logger.info("📱 Telegram（持仓警报）: 发送 %d 条", sent)
            else:
                # 没有 Telegram 时打印到终端
                print("\n" + "═" * 60)
                print("  📊 持仓风控警报")
                print("═" * 60)
                for alert_msg in pos_alerts:
                    # strip HTML tags for terminal display
                    import re
                    clean = re.sub(r"<[^>]+>", "", alert_msg)
                    print(clean)
                    print()
        else:
            logger.info("📊 持仓风控：所有持仓状态正常，无警报")
    else:
        logger.info("📊 持仓风控：当前无活跃持仓")


if __name__ == "__main__":
    main()
