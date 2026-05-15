"""Arbitrage 策略 — Polymarket × Kalshi 跨平台套利。

当两个平台对同一事件的定价出现差异时，可以同时在两边下注锁定无风险利润。

用法
----
    from polymarket_scanner.strategies.arbitrage import arbitrage_strategy
    
    opportunities = arbitrage_strategy(markets, config)
"""

import logging
from typing import List

from ..config import AccountConfig
from ..models import ArbitrageOpportunity, Market

log = logging.getLogger(__name__)


def find_arbitrage_opportunities(
    poly_markets: List[Market],
    kalshi_markets: List[dict],
    config: AccountConfig,
) -> List[ArbitrageOpportunity]:
    """
    在 Polymarket 和 Kalshi 之间寻找套利机会。
    
    条件：
    1. 同一事件（通过标题匹配）
    2. 价格差 >= 2%（spread）
    3. 流动性充足
    """
    opportunities = []
    min_spread = 0.02
    min_liquidity = 10_000
    
    # 建立 Polymarket 市场索引
    poly_index = {}
    for pm in poly_markets:
        if pm.liquidity < min_liquidity:
            continue
        # 提取关键词
        keywords = _extract_keywords(pm.question.lower())
        for kw in keywords:
            if kw not in poly_index:
                poly_index[kw] = []
            poly_index[kw].append(pm)
    
    # 遍历 Kalshi 市场找匹配
    for km in kalshi_markets:
        title = km.get("title", "").lower()
        keywords = _extract_keywords(title)
        
        best_match = None
        best_score = 0
        
        for kw in keywords:
            for pm in poly_index.get(kw, []):
                score = _match_score(km, pm)
                if score > best_score:
                    best_score = score
                    best_match = pm
        
        if not best_match or best_score < 2:
            continue
        
        # 获取价格
        kalshi_prices = _get_kalshi_prices(km)
        poly_price = best_match.price
        
        # 计算价差
        # Polymarket YES vs Kalshi NO
        spread_yes_no = (poly_price + (1 - kalshi_prices["no_mid"]))
        spread_no_yes = ((1 - poly_price) + kalshi_prices["yes_mid"])
        
        if spread_yes_no < 0.98:
            # Polymarket YES + Kalshi NO < 1，可以套利
            spread = 1 - spread_yes_no
            side = "YES"
            poly_side = "YES"
            kalshi_side = "NO"
            poly_entry = poly_price
            kalshi_entry = kalshi_prices["no_mid"]
        elif spread_no_yes < 0.98:
            # Polymarket NO + Kalshi YES < 1，可以套利
            spread = 1 - spread_no_yes
            side = "NO"
            poly_side = "NO"
            kalshi_side = "YES"
            poly_entry = 1 - poly_price
            kalshi_entry = kalshi_prices["yes_mid"]
        else:
            continue
        
        # 计算仓位
        max_bet = config.total_capital * config.max_position_ratio
        suggested_position = min(max_bet, spread * config.total_capital * 10)  # 放大仓位
        expected_profit = spread * suggested_position * 2  # 两边各下
        
        if expected_profit < config.min_absolute_profit:
            continue
        
        # 置信度
        if spread >= 0.05:
            confidence = "HIGH"
        elif spread >= 0.03:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
        
        opp = ArbitrageOpportunity(
            poly_market=best_match,
            kalshi_data=km,
            poly_side=poly_side,
            poly_price=poly_entry,
            kalshi_price=kalshi_entry,
            spread=spread,
            confidence=confidence,
            suggested_position=suggested_position,
            expected_profit=expected_profit,
            rationale=[
                f"spread: {spread:.1%}",
                f"poly {poly_side} @ {poly_entry:.3f}",
                f"kalshi {kalshi_side} @ {kalshi_entry:.3f}",
            ],
        )
        opportunities.append(opp)
    
    opportunities.sort(key=lambda x: x.spread, reverse=True)
    log.debug("Arbitrage: %d opportunities", len(opportunities))
    return opportunities


def arbitrage_strategy(
    markets: List[Market],
    cfg: AccountConfig,
) -> List[ArbitrageOpportunity]:
    """
    主入口：尝试从 Polymarket 和 Kalshi 寻找套利机会。
    
    如果无法获取 Kalshi 数据，返回空列表。
    """
    try:
        from polymarket_scanner.kalshi_client import KalshiClient
        
        kalshi = KalshiClient()
        if not kalshi.health_check():
            log.warning("Kalshi API 不可用，套利扫描跳过")
            return []
        
        kalshi_markets = kalshi.fetch_active_markets(limit=100)
        if not kalshi_markets:
            log.warning("Kalshi 无活跃市场")
            return []
        
        return find_arbitrage_opportunities(markets, kalshi_markets, cfg)
    
    except ImportError:
        log.warning("kalshi_client 模块不可用")
        return []


def _extract_keywords(text: str) -> List[str]:
    """提取关键词。"""
    stopwords = {
        "the", "a", "an", "is", "are", "will", "be", "to", "of", "in", "on",
        "by", "for", "at", "or", "and", "if", "it", "as", "than", "that",
        "this", "what", "who", "when", "where", "how", "yes", "no",
    }
    words = text.lower().split()
    return [
        w.strip("?.,!\"'()[]{}")
        for w in words
        if len(w) > 2 and w not in stopwords
    ]


def _match_score(kalshi: dict, poly: Market) -> int:
    """计算匹配分数。"""
    k_title = kalshi.get("title", "").lower()
    p_question = poly.question.lower()
    
    k_keywords = set(_extract_keywords(k_title))
    p_keywords = set(_extract_keywords(p_question))
    
    return len(k_keywords & p_keywords)


def _get_kalshi_prices(kalshi_market: dict) -> dict:
    """从 Klasihi 市场数据提取价格。"""
    # 价格是 cents，需要除以 100
    yes_bid = kalshi_market.get("yes_bid", 0) / 100
    yes_ask = kalshi_market.get("yes_ask", 0) / 100
    no_bid = kalshi_market.get("no_bid", 0) / 100
    no_ask = kalshi_market.get("no_ask", 0) / 100
    
    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "yes_mid": (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else 0.5,
        "no_mid": (no_bid + no_ask) / 2 if no_bid and no_ask else 0.5,
    }