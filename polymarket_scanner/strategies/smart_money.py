"""Smart Money 策略 — 追踪聪明钱流向。

识别模式：
- 成交量突然放大（volume spike）
- 价格单边移动（price momentum）
- 大户入场迹象

用法
----
    from polymarket_scanner.strategies.smart_money import smart_money_strategy
    
    opportunities = smart_money_strategy(markets, config)
"""

import logging
from typing import List

from ..config import AccountConfig
from ..models import Market, SmartMoneyOpportunity

log = logging.getLogger(__name__)


def smart_money_strategy(
    markets: List[Market],
    cfg: AccountConfig,
) -> List[SmartMoneyOpportunity]:
    """
    识别聪明钱信号。
    
    条件：
    1. volume_24h >= volume_prev_24h * 2（成交量翻倍）
    2. volume_24h >= 100K
    3. 价格移动 >= 2%
    4. vol/liquidity >= 0.3
    """
    opportunities = []
    
    # 阈值
    min_volume = max(100_000, cfg.stable_min_liquidity * 0.3)
    min_vol_ratio = 0.3
    min_price_move = 0.02  # 2%
    min_expected_profit = cfg.min_absolute_profit
    
    for m in markets:
        # 跳过不符合基本条件的
        if m.liquidity < min_volume:
            continue
        
        # 条件1：成交量翻倍
        if m.volume_prev_24h <= 0:
            vol_ratio = 1.0 if m.volume_24h > 0 else 0
        else:
            vol_ratio = m.volume_24h / m.volume_prev_24h
        
        if vol_ratio < 2.0:
            continue
        
        # 条件2：价格移动
        if abs(m.price_change_24h) < min_price_move:
            continue
        
        # 条件3：vol/liquidity 比例
        if m.liquidity > 0:
            vol_liq_ratio = m.volume_24h / m.liquidity
        else:
            vol_liq_ratio = 0
        
        if vol_liq_ratio < min_vol_ratio:
            continue
        
        # 确定方向
        flow_direction = "BUY" if m.price_change_24h > 0 else "SELL"
        side = "YES" if flow_direction == "BUY" else "NO"
        
        # 计算期望值
        # 如果价格上升（BUY信号），意味着市场认为概率上升
        # 我们的 true_prob 应该高于当前价格
        if side == "YES":
            ev = m.true_prob - m.price
            adj_prob = m.true_prob
        else:
            ev = (1 - m.true_prob) - (1 - m.price)
            adj_prob = 1 - m.true_prob
        
        # Kelly sizing
        if ev > 0:
            kelly_f = ev / (1 / m.price) if m.price > 0 else 0  # 简化
            kelly_f = min(kelly_f, 0.25)  # quarter Kelly
            kelly_bet = kelly_f * cfg.total_capital * cfg.max_position_ratio
            kelly_bet = min(kelly_bet, cfg.total_capital * cfg.max_position_ratio)
        else:
            kelly_bet = 0
        
        expected_profit = ev * kelly_bet if kelly_bet > 0 else 0
        
        if expected_profit < min_expected_profit:
            continue
        
        # 置信度
        if vol_ratio >= 3.0 and abs(m.price_change_24h) >= 0.05:
            confidence = "HIGH"
        elif vol_ratio >= 2.5 and abs(m.price_change_24h) >= 0.03:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
        
        # TP 价格（80% 的 AI edge）
        if side == "YES":
            tp_price = m.price + (ev * 0.8)
        else:
            tp_price = m.price - (ev * 0.8)
        
        opp = SmartMoneyOpportunity(
            market=m,
            flow_direction=flow_direction,
            vol_liq_ratio=vol_liq_ratio,
            price_impact=abs(m.price_change_24h),
            confidence=confidence,
            ev=ev,
            suggested_position=kelly_bet,
            expected_profit=expected_profit,
            side=side,
            kelly_bet=kelly_bet,
            take_profit_price=tp_price,
            rationale=[
                f"volume spike: {vol_ratio:.1f}x",
                f"price move: {m.price_change_24h:+.1%}",
                f"vol/liq: {vol_liq_ratio:.2f}",
            ],
        )
        opportunities.append(opp)
    
    # 按 EV 排序
    opportunities.sort(key=lambda x: x.ev, reverse=True)
    log.debug("Smart Money: %d candidates", len(opportunities))
    return opportunities