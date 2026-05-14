"""市场数据映射器 — 把 Gamma API 原始数据转换为内部 Market 对象。

用法
----
    from polymarket_scanner.mapper import map_markets
    
    raw_data = gamma_client.fetch_active_markets()
    markets = map_markets(raw_data)
"""

import datetime
import logging
from typing import List, Optional

from polymarket_scanner.models import Market

log = logging.getLogger(__name__)


def _parse_float(value, default: float = 0.0) -> float:
    """安全解析浮点数。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_int(value, default: int = 0) -> int:
    """安全解析整数。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_days_to_expiry(raw: dict) -> int:
    """从原始数据解析到期天数。"""
    end_date_str = raw.get("endDate") or raw.get("endDateIso") or ""
    if not end_date_str:
        return 30  # 默认 30 天

    try:
        # 处理 ISO 格式日期
        end_date_str = end_date_str.replace("Z", "+00:00")
        end_dt = datetime.datetime.fromisoformat(end_date_str)
        now = datetime.datetime.now(datetime.timezone.utc)
        days = (end_dt - now).days
        return max(0, days)
    except Exception:
        return 30


def _parse_category(raw: dict) -> str:
    """解析市场分类。"""
    tags = raw.get("tags") or []
    if isinstance(tags, list) and tags:
        first_tag = tags[0]
        if isinstance(first_tag, dict):
            return str(first_tag.get("label") or "general").lower()
        return str(first_tag).lower()
    return str(raw.get("category") or "general").lower()


def _parse_price(raw: dict) -> float:
    """解析当前价格。"""
    # 优先用 outcomePrices
    outcomes = raw.get("outcomePrices") or []
    if isinstance(outcomes, list) and outcomes:
        try:
            price = float(outcomes[0])
            return max(0.0, min(1.0, price))
        except (TypeError, ValueError):
            pass

    # 备选字段
    for field in ["lastTradePrice", "bestBid", "bestAsk"]:
        val = raw.get(field)
        if val is not None:
            try:
                price = float(val)
                return max(0.0, min(1.0, price))
            except (TypeError, ValueError):
                continue

    return 0.5  # 默认


def _parse_bid_ask(raw: dict) -> tuple:
    """解析买卖价差。"""
    outcomes = raw.get("outcomePrices") or []
    if isinstance(outcomes, list) and len(outcomes) >= 2:
        try:
            yes_price = float(outcomes[0])
            no_price = float(outcomes[1])
            # 估算 bid/ask（Polymarket 通常 spread 很小）
            spread = 0.01
            bid = max(0.01, yes_price - spread / 2)
            ask = min(0.99, yes_price + spread / 2)
            return (round(bid, 4), round(ask, 4))
        except (TypeError, ValueError):
            pass
    
    price = _parse_price(raw)
    return (round(max(0.01, price - 0.005), 4), round(min(0.99, price + 0.005), 4))


def map_single_market(raw: dict) -> Optional[Market]:
    """
    把单条 Gamma API 原始数据转换为 Market 对象。
    
    Parameters
    ----------
    raw : dict
        Gamma API 返回的单个市场原始数据
    
    Returns
    -------
    Market or None
        转换成功返回 Market，失败返回 None
    """
    try:
        market_id = str(raw.get("id") or raw.get("conditionId") or "").strip()
        question = str(raw.get("question") or raw.get("title") or "").strip()

        if not market_id or not question:
            return None

        price = _parse_price(raw)
        bid, ask = _parse_bid_ask(raw)
        liquidity = _parse_float(raw.get("liquidity"), 0)
        volume_24h = _parse_float(raw.get("volume24hr") or raw.get("volume24h"), 0)
        volume_prev_24h = _parse_float(raw.get("volume1wk"), 0) / 7  # 周成交量 / 7
        price_change_24h = _parse_float(raw.get("priceChange24h"), 0)
        days_to_expiry = _parse_days_to_expiry(raw)
        category = _parse_category(raw)

        return Market(
            market_id=market_id,
            question=question,
            category=category,
            price=price,
            bid=bid,
            ask=ask,
            liquidity=liquidity,
            volume_24h=volume_24h,
            volume_prev_24h=volume_prev_24h,
            price_change_24h=price_change_24h,
            days_to_expiry=days_to_expiry,
            true_prob=price,  # AI Oracle 后续覆盖
        )
    except Exception as e:
        log.debug("解析市场失败: %s — %r", e, raw)
        return None


def map_markets(raw_list: List[dict]) -> List[Market]:
    """
    批量转换 Gamma API 原始数据为 Market 对象列表。
    
    Parameters
    ----------
    raw_list : List[dict]
        Gamma API 返回的市场原始数据列表
    
    Returns
    -------
    List[Market]
        转换成功的 Market 对象列表（跳过解析失败的）
    """
    markets: List[Market] = []
    skipped = 0

    for raw in raw_list:
        market = map_single_market(raw)
        if market:
            markets.append(market)
        else:
            skipped += 1

    if skipped > 0:
        log.debug("映射完成: %d 成功, %d 跳过", len(markets), skipped)

    return markets
