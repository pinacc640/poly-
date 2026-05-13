#!/usr/bin/env python3
"""live_scanner.py — 生产级 CLI 入口，支持真实 Gamma API + 自动降级 mock + AI Oracle RAG。

用法示例
--------
# 基础模式（自动尝试 Gamma API，失败则降级 mock 数据）：
    python live_scanner.py --capital 70

# 完整 AI Oracle 模式（DeepSeek + Brave 联网搜索）：
    set DEEPSEEK_API_KEY=sk-...
    set BRAVE_API_KEY=BSA...
    python live_scanner.py --use-ai --capital 70

# 强制只用 mock 数据（不尝试 Gamma API）：
    python live_scanner.py --mock --capital 70

# 调试模式（打印原始 API 响应 + 详细日志）：
    python live_scanner.py --use-ai --capital 70 --verbose

CLI 参数一览
-----------
--capital FLOAT       账户总资金，单位 USD（默认 50）
--use-ai              启用 AI Oracle（需要 DEEPSEEK_API_KEY 环境变量）
--mock                强制使用 mock 数据，跳过 Gamma API
--limit INT           从 Gamma API 最多拉取多少个市场（默认 200）
--min-liquidity FLOAT 市场最低流动性过滤（默认 50000 USD）
--timeout INT         Gamma API / AI API 请求超时秒数（默认 15）
--verbose / -v        DEBUG 级别日志

持仓参数（查询无需任何 API Key，只需钱包地址）
--address 0x…         Signer 地址；也可设置 POLY_ADDRESS 环境变量
--show-positions      只打印持仓概览后退出（不运行扫描）
--no-position-filter  禁用持仓去重

AI Oracle 参数（仅 --use-ai 时生效）
--model STR           DeepSeek 模型名（默认 deepseek-chat）
--max-results INT     Brave Search 每个市场返回条数（默认 5）
--temperature FLOAT   DeepSeek 采样温度（默认 0.2）
--max-tokens INT      DeepSeek 最大返回 token 数（默认 256）
--no-fallback         AI 出错时直接报错（默认静默保留原概率）
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
from polymarket_scanner.positions import PositionFetcher
from polymarket_scanner.scanner import MarketScanner

# ---------------------------------------------------------------------------
# Gamma API 常量
# ---------------------------------------------------------------------------
GAMMA_BASE_URL       = "https://gamma-api.polymarket.com"
GAMMA_MARKETS_URL    = f"{GAMMA_BASE_URL}/markets"
GAMMA_TIMEOUT        = 10   # 连接超时秒数，超时直接降级


# ---------------------------------------------------------------------------
# Gamma API — 拉取真实市场数据
# ---------------------------------------------------------------------------

def _gamma_fetch_raw(limit: int, timeout: int) -> List[dict]:
    """向 Gamma API 请求活跃市场，返回原始 dict 列表。"""
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
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Gamma API HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Gamma API 连接失败: {e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"Gamma API 未知错误: {e}") from e


def _parse_gamma_market(raw: dict) -> Optional[Market]:
    """把 Gamma API 的单条 market dict 转成内部 Market 对象，解析失败返回 None。"""
    try:
        market_id = str(raw.get("id") or raw.get("conditionId") or "").strip()
        question  = str(raw.get("question") or raw.get("title") or "").strip()
        if not market_id or not question:
            return None

        # 价格
        outcomes = raw.get("outcomePrices") or []
        if isinstance(outcomes, list) and len(outcomes) >= 1:
            price = float(outcomes[0])
        else:
            price = float(raw.get("lastTradePrice") or 0.5)
        price = max(0.0, min(1.0, price))

        # 流动性 / 成交量
        liquidity        = float(raw.get("liquidity")     or 0)
        volume_24h       = float(raw.get("volume24hr")    or 0)
        volume_prev_24h  = float(raw.get("volume1wk")     or 0) / 7
        price_change_24h = float(raw.get("priceChange24h")or 0)

        # 到期天数
        end_date_str = raw.get("endDate") or raw.get("endDateIso") or ""
        if end_date_str:
            try:
                end_dt = datetime.datetime.fromisoformat(
                    end_date_str.replace("Z", "+00:00")
                )
                now = datetime.datetime.now(datetime.timezone.utc)
                days_to_expiry = max(0, (end_dt - now).days)
            except Exception:
                days_to_expiry = 30
        else:
            days_to_expiry = 30

        # 分类
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
            true_prob        = price,   # AI Oracle 后续覆盖
        )
    except Exception:
        return None


def fetch_live_markets(
    limit: int,
    min_liquidity: float,
    timeout: int,
    logger: logging.Logger,
) -> Optional[List[Market]]:
    """从 Gamma API 拉取真实市场。成功返回列表，失败返回 None（调用方降级 mock）。"""
    logger.info("正在连接 Gamma API，拉取最多 %d 个市场…", limit)
    try:
        raw_list = _gamma_fetch_raw(limit, timeout=min(timeout, GAMMA_TIMEOUT))
    except RuntimeError as e:
        logger.warning("Gamma API 不可用 (%s)，将自动降级为 mock 数据。", e)
        return None

    markets: List[Market] = []
    for raw in raw_list:
        m = _parse_gamma_market(raw)
        if m and m.liquidity >= min_liquidity:
            markets.append(m)

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
        description="Polymarket Market Scanner — 真实数据 + AI Oracle 版",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--capital",       type=float, default=50.0,   metavar="USD",
                   help="账户总资金 USD（默认 50）")
    p.add_argument("--use-ai",        action="store_true", default=False,
                   help="启用 AI Oracle（需要 DEEPSEEK_API_KEY）")
    p.add_argument("--mock",          action="store_true", default=False,
                   help="强制使用 mock 数据，跳过 Gamma API")
    p.add_argument("--limit",         type=int,   default=200,    metavar="N",
                   help="Gamma API 最多拉取市场数（默认 200）")
    p.add_argument("--min-liquidity", type=float, default=50_000, metavar="USD",
                   dest="min_liquidity",
                   help="最低流动性过滤（默认 50000）")
    p.add_argument("--timeout",       type=int,   default=15,     metavar="SEC",
                   help="网络请求超时秒数（默认 15）")
    p.add_argument("--verbose", "-v", action="store_true", default=False,
                   help="DEBUG 级别日志")

    wallet = p.add_argument_group(
        "持仓参数",
        "持仓查询使用 Polymarket 公开 Data API，只需提供钱包地址即可（无需私钥或签名）。\n"
        "Relayer API Key 用于下单，与查询持仓无关。",
    )
    wallet.add_argument(
        "--address", type=str, default=None, dest="address",
        metavar="0x…",
        help=(
            "Signer / Proxy 钱包地址（0x 格式）。\n"
            "也可设置环境变量 POLY_ADDRESS。\n"
            "例：0x1139Fe3b54cF43A2aAD1E6E8C09aedf73E5270bf"
        ),
    )
    wallet.add_argument(
        "--show-positions", action="store_true", default=False,
        dest="show_positions",
        help="只打印当前持仓概览后退出，不运行市场扫描。",
    )
    wallet.add_argument(
        "--no-position-filter", action="store_true", default=False,
        dest="no_position_filter",
        help="禁用持仓去重过滤（已持仓的市场也会出现在推荐列表中）。",
    )

    ai = p.add_argument_group("AI Oracle 参数（仅 --use-ai 时生效）")
    ai.add_argument("--model",        type=str,   default="deepseek-chat",
                    help="DeepSeek 模型名（默认 deepseek-chat）")
    ai.add_argument("--max-results",  type=int,   default=5, dest="max_results",
                    help="Brave Search 每市场条数（默认 5）")
    ai.add_argument("--temperature",  type=float, default=0.2,
                    help="DeepSeek 采样温度（默认 0.2）")
    ai.add_argument("--max-tokens",   type=int,   default=256, dest="max_tokens",
                    help="DeepSeek 最大 token 数（默认 256）")
    ai.add_argument("--no-fallback",  action="store_true", default=False,
                    help="AI 出错时抛异常（默认静默保留原概率）")
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

    # ── 1. 账户配置 ──────────────────────────────────────────────────────────
    cfg = AccountConfig(total_capital=args.capital)
    logger.info("账户资金: $%.2f", args.capital)

    # ── 2. 持仓查询（公开 Data API，只需地址）────────────────────────────────
    fetcher = PositionFetcher(
        address = args.address,
        timeout = args.timeout,
    )

    # --show-positions 模式：只打印持仓，不扫描
    if args.show_positions:
        fetcher.print_summary()
        return

    held_ids: set = set()
    if not args.no_position_filter:
        held_ids = fetcher.held_market_ids()
        if held_ids:
            logger.info("🔒 已持仓 %d 个市场，扫描将跳过这些标的（防止重复买入）。", len(held_ids))
        else:
            logger.info("📭 当前无持仓，全量扫描。")
    else:
        logger.info("--no-position-filter: 持仓去重已禁用。")

    # ── 3. 获取市场数据 ───────────────────────────────────────────────────────
    using_mock = False

    if args.mock:
        logger.info("--mock 模式：直接使用 mock 数据")
        markets    = load_mock_markets()
        using_mock = True
    else:
        markets = fetch_live_markets(
            limit         = args.limit,
            min_liquidity = args.min_liquidity,
            timeout       = args.timeout,
            logger        = logger,
        )
        if markets is None:
            logger.info("已自动降级为 mock 数据，策略逻辑不受影响。")
            markets    = load_mock_markets()
            using_mock = True

    src_label = "⚠️  mock 数据" if using_mock else "✅ Gamma API 实时数据"
    logger.info("数据来源：%s，共 %d 个市场", src_label, len(markets))

    # ── 4. AI Oracle 增强（可选）─────────────────────────────────────────────
    if args.use_ai:
        from polymarket_scanner.ai_oracle import AIOracle

        print("🔮 AI Oracle 模式 — 正在用 DeepSeek + Brave Search 评估概率…\n")
        logger.info("AI Oracle: model=%s  timeout=%ds  max_results=%d  temperature=%.2f",
                    args.model, args.timeout, args.max_results, args.temperature)
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

    # ── 5. 运行扫描器（含持仓去重）───────────────────────────────────────────
    scanner = MarketScanner(cfg=cfg, data_source=lambda: markets, held_market_ids=held_ids)
    report  = scanner.run()

    # ── 6. 输出报告 ───────────────────────────────────────────────────────────
    if using_mock:
        print("⚠️  注意：Gamma API 不可用，当前结果基于 mock 数据\n")
    if report.already_held_skipped:
        print(f"🚫 已跳过 {report.already_held_skipped} 个持仓中的市场（防止重复推送）\n")
    print(format_report(report))


if __name__ == "__main__":
    main()
