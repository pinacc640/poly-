#!/usr/bin/env python3
"""live_scanner.py — 生产级 CLI 入口。

Phase 3 additions
-----------------
- Step 6: Telegram push via TelegramNotifier (reads TELEGRAM_BOT_TOKEN +
  TELEGRAM_CHAT_ID from environment; silently skips if not configured)
- report.ai_oracle_used flag is now set before format_report()
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
# Gamma API constants
# ---------------------------------------------------------------------------
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
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
    req = urllib.request.Request(
        f"{GAMMA_MARKETS_URL}?{params}",
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
        price = (
            float(outcomes[0])
            if isinstance(outcomes, list) and outcomes
            else float(raw.get("lastTradePrice") or 0.5)
        )
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
            category = (
                tags[0].get("label") if isinstance(tags[0], dict) else str(tags[0])
            ).lower()
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
        logger.warning("Gamma API 不可用 (%s) — 降级为 mock 数据。", e)
        return None

    markets = [
        m for raw in raw_list
        if (m := _parse_gamma_market(raw)) and m.liquidity >= min_liquidity
    ]

    if not markets:
        logger.warning("Gamma API 返回 0 条有效市场 — 降级为 mock 数据。")
        return None

    logger.info("✅ Gamma API: %d 条市场 (流动性 >= $%.0f)", len(markets), min_liquidity)
    return markets


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="live_scanner",
        description="Polymarket Market Scanner — Phase 3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--capital",       type=float, default=50.0,   metavar="USD")
    p.add_argument("--use-ai",        action="store_true", default=False)
    p.add_argument("--mock",          action="store_true", default=False)
    p.add_argument("--limit",         type=int,   default=200,    metavar="N")
    p.add_argument("--min-liquidity", type=float, default=50_000, metavar="USD",
                   dest="min_liquidity")
    p.add_argument("--timeout",       type=int,   default=15,     metavar="SEC")
    p.add_argument("--verbose", "-v", action="store_true", default=False)

    ai = p.add_argument_group("AI Oracle (仅 --use-ai 时生效)")
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
    args = _build_parser().parse_args()
    logging.basicConfig(
        level   = logging.DEBUG if args.verbose else logging.INFO,
        format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt = "%H:%M:%S",
    )
    logger = logging.getLogger(__name__)

    # ── 1. Config ────────────────────────────────────────────────────────────
    cfg = AccountConfig(total_capital=args.capital)
    logger.info("账户资金: $%.2f", args.capital)

    # ── 2. Market data ───────────────────────────────────────────────────────
    using_mock = False

    if args.mock:
        logger.info("--mock 模式")
        markets    = load_mock_markets()
        using_mock = True
    else:
        markets = fetch_live_markets(
            limit=args.limit, min_liquidity=args.min_liquidity,
            timeout=args.timeout, logger=logger,
        )
        if markets is None:
            logger.info("已降级为 mock 数据。")
            markets    = load_mock_markets()
            using_mock = True

    logger.info(
        "数据来源: %s，共 %d 个市场",
        "⚠️  mock" if using_mock else "✅ Gamma API",
        len(markets),
    )

    # ── 3. AI Oracle (optional) ──────────────────────────────────────────────
    if args.use_ai:
        from polymarket_scanner.ai_oracle import AIOracle
        print("🔮 AI Oracle — DeepSeek + Brave Search…\n")
        try:
            oracle = AIOracle(
                fallback_on_error = not args.no_fallback,
                timeout           = args.timeout,
                model             = args.model,
                max_results       = args.max_results,
                temperature       = args.temperature,
                max_tokens        = args.max_tokens,
            )
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            sys.exit(1)
        markets = oracle.enrich_all(markets)
        logger.info("AI Oracle 增强完成")

    # ── 4. Run scanner ───────────────────────────────────────────────────────
    scanner = MarketScanner(cfg=cfg, data_source=lambda: markets)
    report  = scanner.run()
    report.ai_oracle_used = args.use_ai

    # ── 5. Print report ──────────────────────────────────────────────────────
    if using_mock:
        print("⚠️  注意: 当前结果基于 mock 数据\n")
    print(format_report(report))

    # ── 6. Telegram push (if configured) ────────────────────────────────────
    from polymarket_scanner.notifier import TelegramNotifier
    notifier = TelegramNotifier()
    if notifier.is_enabled():
        total_approved = (
            len(report.stable_approved)
            + len(report.volatility_approved)
            + len(report.smart_money_approved)
        )
        if total_approved > 0:
            sent = notifier.send_report(report)
            logger.info("📱 Telegram: 已发送 %d 条推送", sent)
        else:
            logger.info("📱 Telegram: 无合规机会，跳过推送")
    else:
        logger.debug("Telegram 未配置，跳过推送")


if __name__ == "__main__":
    main()
