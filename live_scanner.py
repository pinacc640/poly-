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
  --limit INT           Gamma API 最多拉取市场数（默认 1000，支持翻页）
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
    format_smart_money_report,
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

# Telegram 通知配置（从环境变量读取）
import os as _os
TELEGRAM_BOT_TOKEN = _os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = _os.getenv("TELEGRAM_CHAT_ID", "")


# ---------------------------------------------------------------------------
# Gamma API helpers
# ---------------------------------------------------------------------------

def _gamma_fetch_raw(limit: int, timeout: int) -> List[dict]:
    """向 Gamma API 请求活跃市场，支持翻页，返回原始 dict 列表。

    Gamma API 单次最多返回 500 条，超过时自动翻页直到达到 limit 或无更多数据。
    """
    all_raw: List[dict] = []
    offset = 0
    page_size = 500  # Gamma API 单页上限

    while len(all_raw) < limit:
        fetch_size = min(page_size, limit - len(all_raw))
        params = urllib.parse.urlencode({
            "active": "true",
            "closed": "false",
            "limit":  fetch_size,
            "offset": offset,
        })
        url = f"{GAMMA_MARKETS_URL}?{params}"
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "polymarket-scanner/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                page = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Gamma API HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Gamma API 连接失败: {e.reason}") from e
        except Exception as e:
            raise RuntimeError(f"Gamma API 未知错误: {e}") from e

        if not page:
            break
        all_raw.extend(page)
        if len(page) < fetch_size:
            break  # 服务端已无更多数据
        offset += len(page)

    return all_raw


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
    logger.info("正在连接 Gamma API，拉取最多 %d 个市场（支持翻页）…", limit)
    try:
        raw_list = _gamma_fetch_raw(limit, timeout=min(timeout, GAMMA_TIMEOUT))
    except RuntimeError as e:
        logger.warning("Gamma API 不可用 (%s)，将自动降级为 mock 数据。", e)
        return None

    markets = [m for raw in raw_list if (m := _parse_gamma_market(raw)) and m.liquidity >= min_liquidity]
    logger.info(
        "Gamma API 原始返回 %d 条，过滤后（流动性 >= $%.0f）%d 条",
        len(raw_list), min_liquidity, len(markets),
    )
    if not markets:
        logger.warning("Gamma API 返回 0 条有效市场，将自动降级为 mock 数据。")
        return None

    logger.info("✅ Gamma API 成功：共 %d 条市场（流动性 >= $%.0f）", len(markets), min_liquidity)
    return markets


# ---------------------------------------------------------------------------
# Telegram 通知助手
# ---------------------------------------------------------------------------

def _send_telegram(token: str, chat_id: str, text: str, logger: logging.Logger) -> None:
    """向 Telegram 发送消息（失败仅警告，不中断主流程）。"""
    if not token or not chat_id:
        logger.debug("Telegram 未配置（TOKEN 或 CHAT_ID 为空），跳过发送。")
        return
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id":    chat_id,
        "text":       text[:4096],   # Telegram 单条消息上限 4096 字符
        "parse_mode": "HTML",
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data    = payload,
        headers = {"Content-Type": "application/json"},
        method  = "POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if result.get("ok"):
            logger.info("✅ Telegram 简报已发送（chat_id=%s）", chat_id)
        else:
            logger.warning("Telegram 发送失败：%s", result)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Telegram 发送异常：%s", exc)


def _build_telegram_summary(
    scan_report,
    monitor_report,
    arb_report,
    markets_total: int,
    using_mock: bool,
) -> str:
    """构建 Telegram 简报文本（HTML 格式）。"""
    import datetime as _dt
    now = _dt.datetime.now().strftime("%m-%d %H:%M")
    lines = [f"<b>📊 Polymarket 简报  [{now}]</b>"]

    src = "⚠️ mock数据" if using_mock else "✅ Gamma API"
    lines.append(f"数据来源：{src}  |  检查市场：<b>{markets_total}</b> 个")
    lines.append("")

    # ── Phase 1：新机会 ────────────────────────────────────────────────
    if scan_report is not None:
        n_stable = len(scan_report.stable_approved)
        n_vol    = len(scan_report.volatility_approved)
        n_held   = scan_report.already_held_skipped
        if n_stable + n_vol > 0:
            lines.append(f"🎯 <b>新机会</b>：稳定 {n_stable} 个  波动 {n_vol} 个")
            for opp, dec in scan_report.stable_approved[:3]:
                pos = dec.approved_position or opp.suggested_position
                lines.append(
                    f"  • [稳定] {opp.market.question[:45]}…\n"
                    f"    EV={opp.ev:+.3f}  仓位=${pos:.0f}  风险={opp.risk_level}"
                )
            for opp, dec in scan_report.volatility_approved[:3]:
                pos = dec.approved_position or opp.suggested_position
                lines.append(
                    f"  • [波动] {opp.market.question[:45]}…\n"
                    f"    进场={opp.entry_price:.3f}→{opp.target_price:.3f}  仓位=${pos:.0f}"
                )
        else:
            lines.append("📭 新机会：暂无符合纪律的交易机会")
        if n_held:
            lines.append(f"🔒 已分流 {n_held} 个持仓市场至监控")
    else:
        lines.append("⏭ Phase 1 扫描已跳过（--no-scan）")

    lines.append("")

    # ── Phase 2a：持仓监控 ─────────────────────────────────────────────
    if monitor_report is not None:
        actionable = monitor_report.actionable
        if actionable:
            lines.append(f"⚡ <b>持仓预警</b>（{len(actionable)} 条需处理）：")
            for sig in actionable[:5]:
                emoji = {"TAKE_PROFIT": "💰", "STOP_LOSS": "🛑", "ADD_POSITION": "➕"}.get(
                    sig.signal_type, "•"
                )
                q = sig.position.question[:40] if sig.position.question else sig.position.market_id[:20]
                lines.append(
                    f"  {emoji} [{sig.signal_type}] {q}…\n"
                    f"    均价={sig.position.avg_price:.3f}  现价={sig.position.current_price:.3f}"
                    f"  PnL=${sig.position.unrealized_pnl:+.2f}"
                )
        else:
            lines.append(f"✅ 持仓监控：{monitor_report.positions_checked} 个持仓均正常，无需操作")
    else:
        lines.append("⏭ 持仓监控未启用")

    lines.append("")

    # ── Phase 2b：套利 ─────────────────────────────────────────────────
    if arb_report is not None:
        opps = arb_report.opportunities
        if opps:
            lines.append(f"⚡ <b>套利机会</b>（{len(opps)} 个，最大空间 {opps[0].arb_gap:.2%}）：")
            for opp in opps[:3]:
                lines.append(
                    f"  • {opp.pm_market.question[:40]}…\n"
                    f"    空间={opp.arb_gap:.2%}  {opp.recommended_action}"
                )
        else:
            lines.append(
                f"📭 套利扫描：检查 {arb_report.pm_markets_checked} PM × "
                f"{arb_report.kalshi_markets_fetched} Kalshi，暂无套利机会"
            )
    else:
        lines.append("⏭ 套利扫描未启用")

    lines.append("")

    # ── Smart Money 汇总（仅统计，WHALE ALERT 已通过独立消息推送）────────
    if scan_report is not None:
        sm = scan_report.smart_money
        n_whale = len(sm.whale_alerts)
        n_watch = len(sm.watch_signals)
        if n_whale > 0:
            lines.append(
                f"🐋 <b>Smart Money</b>：{n_whale} 条 WHALE ALERT 已单独推送  |  "
                f"{n_watch} 条 WATCH（见主报告）"
            )
        elif n_watch > 0:
            lines.append(f"👁 Smart Money：{n_watch} 条 WATCH 信号（见主报告）")
        else:
            lines.append("🐋 Smart Money：当前无符合纪律的巨鲸异动")
    else:
        lines.append("⏭ Smart Money 扫描已跳过（--no-scan）")

    return "\n".join(lines)


def _build_smart_money_whale_alert(sm_report) -> str:
    """构建 Smart Money WHALE ALERT Telegram 消息（HTML 格式）。

    仅当存在 WHALE_ALERT 信号时调用。与凯利开仓条件完全解耦，
    标注为"鲸鱼资金行为预警（非开仓信号）"。
    """
    import datetime as _dt
    now = _dt.datetime.now().strftime("%m-%d %H:%M")
    lines = [
        f"<b>🐋 Smart Money — 鲸鱼资金行为预警  [{now}]</b>",
        "<i>⚠️ 以下为纯观察信号，非开仓建议，不依赖 EV / Kelly 条件</i>",
        "",
        f"共发现 <b>{len(sm_report.whale_alerts)}</b> 条 WHALE ALERT：",
        "",
    ]
    for sig in sm_report.whale_alerts[:10]:   # 最多推送 10 条
        q = sig.market.question[:60]
        lines += [
            f"<b>• {q}</b>",
            f"  Vol/Liq = <b>{sig.vol_liq_ratio:.2f}</b>  |  "
            f"24h 价格变动 = <b>{sig.market.price_change_24h:+.2%}</b>",
            f"  当前价格 = {sig.market.price:.3f}  |  "
            f"流动性 = ${sig.market.liquidity:,.0f}",
            "",
        ]
    return "\n".join(lines).strip()


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
    p.add_argument("--limit",         type=int,   default=1000,     metavar="N",
                   help="Gamma API 最多拉取市场数（默认 1000，支持翻页）")
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

    # ── Telegram 参数 ─────────────────────────────────────────────────────
    tg = p.add_argument_group(
        "Telegram 参数",
        "程序结束时发送简报。Token/ChatID 可通过 CLI 或环境变量 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 配置。",
    )
    tg.add_argument("--tg-token",   type=str, default=None, dest="tg_token",
                    metavar="TOKEN", help="Telegram Bot Token（覆盖环境变量）")
    tg.add_argument("--tg-chat",    type=str, default=None, dest="tg_chat",
                    metavar="CHAT_ID", help="Telegram Chat ID（覆盖环境变量）")
    tg.add_argument("--no-telegram", action="store_true", default=False, dest="no_telegram",
                    help="禁用 Telegram 通知")

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

    # ── 1b. Telegram 配置 ────────────────────────────────────────────────
    tg_token   = getattr(args, "tg_token",  None) or TELEGRAM_BOT_TOKEN
    tg_chat_id = getattr(args, "tg_chat",   None) or TELEGRAM_CHAT_ID
    tg_token   = (tg_token   or "").strip()
    tg_chat_id = (tg_chat_id or "").strip()
    no_tg      = getattr(args, "no_telegram", False)
    tg_enabled = bool(tg_token and tg_chat_id and not no_tg)
    if tg_enabled:
        logger.info("📢 Telegram 通知已启用（chat_id=%s…）", tg_chat_id[:8])
    else:
        logger.info("📢 Telegram 通知未启用（未配置 token/chat_id 或传了 --no-telegram）")

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

    # ── 5. Phase 1：新机会扫描 ────────────────────────────────────────────
    # --no-scan 拦截必须在 AI enrich 之前：跳过扫描时绝不对非持仓市场调用 AI
    scan_report   = None
    held_markets: list = []

    if not args.no_scan:
        # 只有正常扫描模式才对新机会市场做 AI 增强
        if oracle is not None:
            markets = oracle.enrich_all(markets, ai_top_n=args.ai_top_n)
            logger.info("AI Oracle 增强完成（新机会）")

        scanner = MarketScanner(
            cfg             = cfg,
            data_source     = lambda: markets,
            held_market_ids = held_ids,
        )
        scan_report, held_markets = scanner.run()
    else:
        # --no-scan：完全跳过新机会 AI 增强和策略扫描
        # 仍需从全量市场中分离出已持仓市场，供 Phase 2 监控使用
        held_index   = {p.market_id for p in positions}
        held_markets = [m for m in markets if m.market_id in held_index]
        logger.info("--no-scan: 跳过 Phase 1 新机会 AI 增强和扫描。")

    # ── 6. Phase 2a：持仓动态监控 ────────────────────────────────────────
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

    # ── 7. Phase 2b：跨平台套利扫描 ─────────────────────────────────────
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

    # ── 8. 输出所有报告 ───────────────────────────────────────────────────
    separator = "\n" + "═" * 60 + "\n"

    if using_mock:
        print("⚠️  注意：当前结果基于 mock 数据（Gamma API 不可用）\n")

    # Phase 1：新机会报告（含 Smart Money 观察层）
    if scan_report is not None:
        if scan_report.already_held_skipped:
            print(f"🚫 已分流 {scan_report.already_held_skipped} 个持仓市场至监控模块\n")
        print(format_report(scan_report))   # Smart Money 分区已内嵌在 format_report 中

    # Phase 2a：持仓监控报告
    if monitor_report is not None:
        print(separator)
        print(format_monitor_report(monitor_report))

    # Phase 2b：套利报告
    if arb_report is not None:
        print(separator)
        print(format_arb_report(arb_report))

    # 若什么都没运行（但 Smart Money 可能独立输出）
    if scan_report is None and monitor_report is None and arb_report is None:
        print("未运行任何扫描模块。请加上 --use-ai / --monitor / --arb 参数之一。")

    # ── 8b. Smart Money WHALE ALERT 独立 Telegram 预警 ───────────────────
    # 不依赖凯利/EV 开仓条件，满足阈值即推送，与主简报解耦
    sm_report = scan_report.smart_money if scan_report is not None else None
    if tg_enabled and sm_report is not None and sm_report.whale_alerts:
        logger.info("🐋 发现 %d 条 WHALE ALERT，独立推送 Telegram 预警…",
                    len(sm_report.whale_alerts))
        whale_text = _build_smart_money_whale_alert(sm_report)
        _send_telegram(tg_token, tg_chat_id, whale_text, logger)

    # ── 9. Telegram 主简报（无论有无机会，只要启用就发送）────────────────
    if tg_enabled:
        summary = _build_telegram_summary(
            scan_report    = scan_report,
            monitor_report = monitor_report,
            arb_report     = arb_report,
            markets_total  = len(markets),
            using_mock     = using_mock,
        )
        _send_telegram(tg_token, tg_chat_id, summary, logger)


if __name__ == "__main__":
    main()
