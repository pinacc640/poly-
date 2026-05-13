#!/usr/bin/env python3
"""live_scanner.py — 生产级 CLI 入口 (Phase 1 + Phase 2)

Phase 1（单边扫描）
  • 从 Gamma API 拉取活跃市场（失败自动降级 mock）
  • AI Oracle（DeepSeek + Brave）Top-N 概率增强
  • MarketScanner 策略评估 + 风控审批
  • 输出稳定套利 / 波动套利机会报告

Phase 2 新增（--monitor / --arb）
  • --monitor  持仓动态监控：对已持仓市场 AI 增强，生成止盈 / 止损 / 加仓信号
  • --arb      跨平台套利扫描：Polymarket × Kalshi Jaccard+AI 匹配，发现无风险套利空间

用法示例
--------
# 基础扫描（Phase 1）：
    python live_scanner.py --capital 70

# 完整 AI + 持仓监控 + 套利扫描（全功能）：
    python live_scanner.py --use-ai --monitor --arb --capital 70

# 只看持仓信号（不扫描新机会）：
    python live_scanner.py --monitor --no-scan

# 只做套利扫描：
    python live_scanner.py --arb --no-scan

# 强制 mock 数据（离线调试）：
    python live_scanner.py --use-ai --monitor --arb --mock

CLI 参数一览
-----------
基础参数
  --capital FLOAT       账户总资金 USD（默认 50）
  --use-ai              启用 AI Oracle（需要 DEEPSEEK_API_KEY）
  --mock                强制 mock 数据，跳过 Gamma API
  --no-scan             跳过 Phase 1 新机会扫描（仅运行 Phase 2 模块）
  --limit INT           Gamma API 最多拉取市场数（默认 200）
  --min-liquidity FLOAT 市场最低流动性过滤（默认 50000 USD）
  --timeout INT         网络请求超时秒数（默认 15）
  --verbose / -v        DEBUG 日志

持仓参数
  --address 0x…         Proxy/Signer 钱包地址（或设 POLY_ADDRESS 环境变量）
  --show-positions      只打印持仓概览后退出
  --no-position-filter  禁用持仓去重

Phase 2 参数
  --monitor             启用持仓动态监控
  --arb                 启用 Kalshi 跨平台套利扫描
  --arb-min-gap FLOAT   套利最小利润空间（默认 0.05）
  --kalshi-api-key STR  Kalshi API Key（或设 KALSHI_API_KEY 环境变量）

AI Oracle 参数（仅 --use-ai 时生效）
  --model STR           DeepSeek 模型名（默认 deepseek-chat）
  --max-results INT     Brave Search 每市场条数（默认 5）
  --temperature FLOAT   采样温度（默认 0.2）
  --max-tokens INT      最大 token 数（默认 256）
  --no-fallback         AI 出错时抛异常（默认静默保留原概率）
  --ai-top-n INT        新机会 AI 深度分析数量上限（默认 10）
  --monitor-ai-top-n INT 持仓监控 AI 深度分析数量上限（默认 10）
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
from polymarket_scanner.formatter import (
    format_arb_report,
    format_monitor_report,
    format_report,
)
from polymarket_scanner.mock_data import load_mock_markets
from polymarket_scanner.models import Market
from polymarket_scanner.positions import PositionFetcher
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
        url, headers={"Accept": "application/json", "User-Agent": "polymarket-scanner/1.0"},
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
                end_dt = datetime.datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                days_to_expiry = max(0, (end_dt - datetime.datetime.now(datetime.timezone.utc)).days)
            except Exception:
                pass

        tags = raw.get("tags") or []
        if isinstance(tags, list) and tags:
            category = (tags[0].get("label") if isinstance(tags[0], dict) else str(tags[0])).lower()
        else:
            category = str(raw.get("category") or "general").lower()

        return Market(
            market_id=market_id, question=question, category=category,
            price=price, liquidity=liquidity, volume_24h=volume_24h,
            volume_prev_24h=volume_prev_24h, price_change_24h=price_change_24h,
            days_to_expiry=days_to_expiry, true_prob=price,
        )
    except Exception:
        return None


def fetch_live_markets(
    limit: int, min_liquidity: float, timeout: int, logger: logging.Logger,
) -> Optional[List[Market]]:
    logger.info("正在连接 Gamma API，拉取最多 %d 个市场…", limit)
    try:
        raw_list = _gamma_fetch_raw(limit, timeout=min(timeout, GAMMA_TIMEOUT))
    except RuntimeError as e:
        logger.warning("Gamma API 不可用 (%s)，将自动降级为 mock 数据。", e)
        return None

    markets = [m for raw in raw_list if (m := _parse_gamma_market(raw)) and m.liquidity >= min_liquidity]
    if not markets:
        logger.warning("Gamma API 返回 0 条有效市场，将自动降级为 mock 数据。")
        return None

    logger.info("✅ Gamma API 成功：共 %d 条市场（流动性 >= $%.0f）", len(markets), min_liquidity)
    return markets


# ---------------------------------------------------------------------------
# CLI 参数解析
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="live_scanner",
        description="Polymarket Scanner — Phase 1（单边扫描）+ Phase 2（监控 + 套利）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── 基础参数 ──────────────────────────────────────────────────────────
    p.add_argument("--capital",       type=float, default=50.0,    metavar="USD",
                   help="账户总资金 USD（默认 50）")
    p.add_argument("--use-ai",        action="store_true", default=False,
                   help="启用 AI Oracle（需要 DEEPSEEK_API_KEY）")
    p.add_argument("--mock",          action="store_true", default=False,
                   help="强制 mock 数据，跳过 Gamma API")
    p.add_argument("--no-scan",       action="store_true", default=False, dest="no_scan",
                   help="跳过 Phase 1 新机会扫描（仅运行 --monitor / --arb）")
    p.add_argument("--limit",         type=int,   default=200,     metavar="N",
                   help="Gamma API 最多拉取市场数（默认 200）")
    p.add_argument("--min-liquidity", type=float, default=50_000,  metavar="USD",
                   dest="min_liquidity", help="最低流动性过滤（默认 50000）")
    p.add_argument("--timeout",       type=int,   default=15,      metavar="SEC",
                   help="网络请求超时秒数（默认 15）")
    p.add_argument("--verbose", "-v", action="store_true", default=False,
                   help="DEBUG 级别日志")

    # ── 持仓参数 ──────────────────────────────────────────────────────────
    wallet = p.add_argument_group(
        "持仓参数",
        "持仓查询使用 Polymarket 公开 Data API，只需钱包地址（无需私钥）。",
    )
    wallet.add_argument("--address", type=str, default=None, dest="address",
                        metavar="0x…",
                        help="Proxy/Signer 地址；也可设置 POLY_ADDRESS 环境变量")
    wallet.add_argument("--show-positions", action="store_true", default=False,
                        dest="show_positions", help="只打印持仓概览后退出")
    wallet.add_argument("--no-position-filter", action="store_true", default=False,
                        dest="no_position_filter", help="禁用持仓去重过滤")

    # ── Phase 2 参数 ──────────────────────────────────────────────────────
    p2 = p.add_argument_group(
        "Phase 2 参数",
        "持仓动态监控（--monitor）和 Kalshi 跨平台套利扫描（--arb）。",
    )
    p2.add_argument("--monitor",         action="store_true", default=False,
                    help="启用持仓动态监控（止盈 / 止损 / 加仓信号）")
    p2.add_argument("--arb",             action="store_true", default=False,
                    help="启用 Kalshi × Polymarket 跨平台套利扫描")
    p2.add_argument("--arb-min-gap",     type=float, default=0.05, dest="arb_min_gap",
                    metavar="FLOAT", help="套利最小利润空间（默认 0.05）")
    p2.add_argument("--kalshi-api-key",  type=str, default=None, dest="kalshi_api_key",
                    metavar="KEY",
                    help="Kalshi API Key（或设 KALSHI_API_KEY 环境变量；公开端点无需）")

    # ── AI Oracle 参数 ────────────────────────────────────────────────────
    ai = p.add_argument_group("AI Oracle 参数（仅 --use-ai 时生效）")
    ai.add_argument("--model",            type=str,   default="deepseek-chat",
                    help="DeepSeek 模型名（默认 deepseek-chat）")
    ai.add_argument("--max-results",      type=int,   default=5, dest="max_results",
                    help="Brave Search 每市场条数（默认 5）")
    ai.add_argument("--temperature",      type=float, default=0.2,
                    help="DeepSeek 采样温度（默认 0.2）")
    ai.add_argument("--max-tokens",       type=int,   default=256, dest="max_tokens",
                    help="DeepSeek 最大 token 数（默认 256）")
    ai.add_argument("--no-fallback",      action="store_true", default=False,
                    help="AI 出错时抛异常（默认静默保留原概率）")
    ai.add_argument("--ai-top-n",         type=int,   default=10, dest="ai_top_n",
                    help="新机会 AI 深度分析市场数上限（默认 10）")
    ai.add_argument("--monitor-ai-top-n", type=int,   default=10, dest="monitor_ai_top_n",
                    help="持仓监控 AI 深度分析数量上限（默认 10）")

    return p


# ---------------------------------------------------------------------------
# 主函数
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

    # ── 1. 账户配置 ──────────────────────────────────────────────────────
    # 若启用套利扫描且设了 --arb-min-gap，覆盖默认阈值
    # 若启用持仓监控且设了 --monitor-ai-top-n，覆盖默认阈值
    cfg = AccountConfig(
        total_capital    = args.capital,
        arb_min_gap      = args.arb_min_gap,
        monitor_ai_top_n = args.monitor_ai_top_n,
    )
    logger.info("账户资金: $%.2f", args.capital)

    # ── 2. 持仓查询（公开 Data API）──────────────────────────────────────
    fetcher = PositionFetcher(address=args.address, timeout=args.timeout)

    if args.show_positions:
        fetcher.print_summary()
        return

    # 获取完整 Position 对象列表（Phase 2 monitor 需要）
    positions = []
    held_ids: set = set()
    if not args.no_position_filter or args.monitor:
        positions = fetcher.fetch_positions()
        held_ids  = {p.market_id for p in positions}
        if held_ids:
            logger.info(
                "🔒 已持仓 %d 个市场（新机会扫描跳过，Phase 2 监控中使用）。",
                len(held_ids),
            )
        else:
            logger.info("📭 当前无持仓，全量扫描。")
    else:
        logger.info("--no-position-filter: 持仓去重已禁用。")

    # ── 3. 获取市场数据 ───────────────────────────────────────────────────
    using_mock = False
    if args.mock:
        logger.info("--mock 模式：直接使用 mock 数据")
        markets    = load_mock_markets()
        using_mock = True
    else:
        markets = fetch_live_markets(
            limit=args.limit, min_liquidity=args.min_liquidity,
            timeout=args.timeout, logger=logger,
        )
        if markets is None:
            logger.info("已自动降级为 mock 数据。")
            markets    = load_mock_markets()
            using_mock = True

    logger.info(
        "数据来源：%s，共 %d 个市场",
        "⚠️  mock" if using_mock else "✅ Gamma API",
        len(markets),
    )

    # ── 4. 构建 AI Oracle（若需要）───────────────────────────────────────
    oracle = None
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
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            sys.exit(1)

    # ── 5. AI 增强（新机会部分，Phase 1）─────────────────────────────────
    if oracle is not None:
        markets = oracle.enrich_all(markets, ai_top_n=args.ai_top_n)
        logger.info("AI Oracle 增强完成（新机会）")

    # ── 6. Phase 1：新机会扫描 ────────────────────────────────────────────
    scan_report   = None
    held_markets: list = []

    if not args.no_scan:
        scanner = MarketScanner(
            cfg             = cfg,
            data_source     = lambda: markets,
            held_market_ids = held_ids,
        )
        scan_report, held_markets = scanner.run()
    else:
        # no_scan 模式：仍需分离已持仓市场供 Phase 2 使用
        held_index    = {p.market_id for p in positions}
        held_markets  = [m for m in markets if m.market_id in held_index]
        logger.info("--no-scan: 跳过 Phase 1 新机会扫描。")

    # ── 7. Phase 2a：持仓动态监控 ────────────────────────────────────────
    monitor_report = None
    if args.monitor:
        from polymarket_scanner.position_monitor import PositionMonitor
        if not positions:
            logger.info("--monitor: 无持仓数据，跳过持仓监控。")
        else:
            logger.info("🔍 持仓动态监控：检查 %d 个持仓…", len(positions))
            monitor = PositionMonitor(cfg=cfg)
            monitor_report = monitor.run(
                positions    = positions,
                all_markets  = markets,
                oracle       = oracle,  # None → 用原始市价；有 oracle → AI 增强
            )

    # ── 8. Phase 2b：跨平台套利扫描 ─────────────────────────────────────
    arb_report = None
    if args.arb:
        from polymarket_scanner.arbitrage_scanner import ArbitrageScanner
        logger.info("⚡ Kalshi × Polymarket 套利扫描启动…")
        arb_scanner = ArbitrageScanner(
            cfg              = cfg,
            deepseek_api_key = oracle.deepseek_key if oracle else None,
            kalshi_api_key   = args.kalshi_api_key,
            timeout          = args.timeout,
            model            = args.model,
        )
        arb_report = arb_scanner.scan(markets)

    # ── 9. 输出所有报告 ───────────────────────────────────────────────────
    separator = "\n" + "═" * 60 + "\n"

    if using_mock:
        print("⚠️  注意：当前结果基于 mock 数据（Gamma API 不可用）\n")

    # Phase 1：新机会报告
    if scan_report is not None:
        if scan_report.already_held_skipped:
            print(f"🚫 已分流 {scan_report.already_held_skipped} 个持仓市场至监控模块\n")
        print(format_report(scan_report))

    # Phase 2a：持仓监控报告
    if monitor_report is not None:
        print(separator)
        print(format_monitor_report(monitor_report))

    # Phase 2b：套利报告
    if arb_report is not None:
        print(separator)
        print(format_arb_report(arb_report))

    # 若什么都没运行
    if scan_report is None and monitor_report is None and arb_report is None:
        print("未运行任何扫描模块。请加上 --use-ai / --monitor / --arb 参数之一。")


if __name__ == "__main__":
    main()
