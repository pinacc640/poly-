"""Volatility arbitrage strategy.

Phase 3: VolatilityOpportunity now carries a `side` field.
take_profit_price is stamped by the RiskController (= target_price).
"""

from typing import List

from ..config import AccountConfig, DEFAULT_CONFIG
from ..models import Market, VolatilityOpportunity


def _passes_filters(market: Market, cfg: AccountConfig) -> bool:
    if abs(market.price_change_24h) < cfg.vol_min_abs_price_change_24h:
        return False
    if market.liquidity < cfg.vol_min_liquidity:
        return False
    if market.has_fundamental_change:
        return False
    return True


def _bracket(market: Market, cfg: AccountConfig):
    entry    = market.price
    abs_move = abs(market.price_change_24h)
    dynamic_target = max(cfg.vol_target_move, round(abs_move * 0.60, 3))

    if market.price_change_24h <= -cfg.vol_min_abs_price_change_24h:
        side   = "YES"
        target = min(1.0, entry + dynamic_target)
        stop   = max(0.0, entry - cfg.vol_stop_move)
    else:
        side   = "NO"
        target = max(0.0, entry - dynamic_target)
        stop   = min(1.0, entry + cfg.vol_stop_move)
    return side, entry, target, stop


def _expected_value(
    market: Market,
    side: str,
    entry: float,
    target: float,
    stop: float,
) -> float:
    if side == "YES":
        p_win      = market.true_prob
        win_amount = target - entry
        lose_amount= entry  - stop
    else:
        p_win      = 1 - market.true_prob
        win_amount = entry  - target
        lose_amount= stop   - entry
    return p_win * win_amount - (1 - p_win) * lose_amount


def volatility_strategy(
    markets: List[Market],
    cfg: AccountConfig = DEFAULT_CONFIG,
) -> List[VolatilityOpportunity]:
    opportunities: List[VolatilityOpportunity] = []
    max_position = cfg.total_capital * cfg.max_position_ratio

    for m in markets:
        if not _passes_filters(m, cfg):
            continue

        side, entry, target, stop = _bracket(m, cfg)
        ev = _expected_value(m, side, entry, target, stop)
        if ev <= 0:
            continue

        expected_profit = max_position * ev

        opportunities.append(
            VolatilityOpportunity(
                market             = m,
                entry_price        = round(entry, 4),
                target_price       = round(target, 4),
                stop_loss          = round(stop, 4),
                ev                 = round(ev, 4),
                suggested_position = round(max_position, 2),
                expected_profit    = round(expected_profit, 2),
                max_hold_days      = cfg.vol_max_hold_days,
                side               = side,
                rationale          = [
                    f"side={side}",
                    f"24h move {m.price_change_24h:+.2%}",
                    f"liquidity ${m.liquidity:,.0f}",
                ],
            )
        )

    opportunities.sort(key=lambda o: o.ev, reverse=True)
    return opportunities
