"""Smart Money 2.0 — Whale detection with price-impact filter.

Thesis
------
Real institutional / whale accumulation leaves a signature in the data:
BOTH volume spikes AND price movement. Pure volume without price movement
is wash-trading / fake volume (洗盘). We filter it out with two new checks:

1. Price-Impact Ratio
   price_impact_ratio = abs(price_change_24h) / (volume_24h / liquidity)

   Intuition: if a whale pours $1M into a $2M liquidity pool (ratio=0.5)
   but price only moves 1%, the impact ratio = 0.01 / 0.5 = 0.02 → very low.
   Real buying pressure should move price substantially relative to book size.

2. Breakout Confirmation
   We require abs(price_change_24h) > sm_breakout_threshold (default 10%).
   This filters out markets that are just noisy / choppy.

Confidence levels (combined signal scoring)
-------------------------------------------
HIGH   : volume spike + price impact passes + breakout confirmed + vol/liq ratio high
MEDIUM : volume spike + price impact passes + either breakout OR high vol/liq ratio
LOW    : volume spike only — flagged for monitoring but NOT recommended for entry

Only HIGH and MEDIUM generate actionable SmartMoneyOpportunity proposals.
LOW-confidence markets are dropped (not enough signal for a real trade).
"""

from typing import List

from ..config import AccountConfig, DEFAULT_CONFIG
from ..models import Market, SmartMoneyOpportunity


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _passes_basic_filters(market: Market, cfg: AccountConfig) -> bool:
    """Pre-filter: remove markets that clearly don't qualify."""
    if market.volume_24h < cfg.sm_min_volume_24h:
        return False
    if market.liquidity < cfg.sm_min_liquidity:
        return False
    if market.days_to_expiry > cfg.sm_max_days_to_expiry:
        return False
    return True


# ---------------------------------------------------------------------------
# Price-impact analysis (the core of Smart Money 2.0)
# ---------------------------------------------------------------------------

def _price_impact_ratio(market: Market) -> float:
    """Compute price_impact_ratio = abs(price_change_24h) / (volume_24h / liquidity).

    A high ratio means: for every unit of volume relative to book depth,
    price moved a lot → consistent with real directional conviction.
    A low ratio means: huge volume but price barely moved → suspect.

    Returns 0.0 if liquidity is 0 (safe guard).
    """
    if market.liquidity <= 0:
        return 0.0
    vol_pressure = market.volume_24h / market.liquidity
    if vol_pressure <= 0:
        return 0.0
    return abs(market.price_change_24h) / vol_pressure


def _classify_confidence(
    market: Market,
    cfg: AccountConfig,
    vol_liq_ratio: float,
    price_impact: float,
) -> str:
    """Return "HIGH", "MEDIUM", or "LOW" based on combined signals."""
    has_volume_spike  = vol_liq_ratio >= cfg.sm_min_vol_liq_ratio
    has_price_impact  = price_impact  >= cfg.sm_min_price_impact_ratio
    has_breakout      = abs(market.price_change_24h) >= cfg.sm_breakout_threshold
    has_big_move      = abs(market.price_change_24h) >= cfg.sm_min_price_move

    if has_volume_spike and has_price_impact and has_breakout and has_big_move:
        return "HIGH"
    if has_volume_spike and has_price_impact and (has_breakout or has_big_move):
        return "MEDIUM"
    if has_volume_spike:
        return "LOW"
    return "NONE"


# ---------------------------------------------------------------------------
# EV calculation
# ---------------------------------------------------------------------------

def _compute_ev(market: Market, side: str, confidence: str, cfg: AccountConfig) -> float:
    """EV per $1 of notional following the smart-money flow.

    We boost true_prob by a confidence-based edge factor to account for
    the information content we believe the whale flow contains.
    """
    edge = (
        cfg.sm_high_confidence_edge   if confidence == "HIGH"
        else cfg.sm_medium_confidence_edge
    )

    if side == "BUY":
        # We follow whales buying YES
        adjusted_prob = min(0.99, market.true_prob + edge)
        return adjusted_prob * (1 - market.price) - (1 - adjusted_prob) * market.price
    else:
        # We follow whales buying NO
        adjusted_prob = max(0.01, market.true_prob - edge)
        no_price = 1 - market.price
        return (1 - adjusted_prob) * no_price - adjusted_prob * (1 - no_price)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def smart_money_strategy(
    markets: List[Market],
    cfg: AccountConfig = DEFAULT_CONFIG,
) -> List[SmartMoneyOpportunity]:
    """Scan markets for whale accumulation with price-impact confirmation.

    Returns only HIGH and MEDIUM confidence opportunities.
    LOW confidence (volume spike without price impact) is discarded.
    """
    opportunities: List[SmartMoneyOpportunity] = []
    max_position = cfg.total_capital * cfg.max_position_ratio

    for m in markets:
        if not _passes_basic_filters(m, cfg):
            continue

        # Compute derived metrics
        vol_liq_ratio  = m.volume_24h / m.liquidity if m.liquidity > 0 else 0.0
        price_impact   = _price_impact_ratio(m)
        confidence     = _classify_confidence(m, cfg, vol_liq_ratio, price_impact)

        # Drop LOW and NONE — not actionable
        if confidence not in ("HIGH", "MEDIUM"):
            continue

        # Determine flow direction from price movement
        if m.price_change_24h >= 0:
            flow_direction = "BUY"
            side           = "YES"
        else:
            flow_direction = "SELL"
            side           = "NO"

        ev = _compute_ev(m, flow_direction, confidence, cfg)
        if ev <= 0:
            continue

        expected_profit = max_position * ev
        is_breakout     = abs(m.price_change_24h) >= cfg.sm_breakout_threshold

        # Build rationale notes
        notes = [
            f"side={side}",
            f"flow={flow_direction}",
            f"vol/liq={vol_liq_ratio:.2f}",
            f"price_impact_ratio={price_impact:.3f}",
            f"breakout={'YES' if is_breakout else 'NO'}",
            f"Δ24h={m.price_change_24h:+.2%}",
            f"volume=${m.volume_24h:,.0f}",
        ]
        if not is_breakout:
            notes.append("⚠ no breakout confirmation — use smaller size")

        opportunities.append(
            SmartMoneyOpportunity(
                market              = m,
                confidence          = confidence,
                flow_direction      = flow_direction,
                volume_spike_ratio  = round(vol_liq_ratio, 3),
                price_move_pct      = round(abs(m.price_change_24h), 4),
                price_impact_ratio  = round(price_impact, 4),
                is_breakout         = is_breakout,
                ev                  = round(ev, 4),
                suggested_position  = round(max_position, 2),
                expected_profit     = round(expected_profit, 2),
                side                = side,
                rationale           = notes,
            )
        )

    # Sort: HIGH before MEDIUM, then by EV
    opportunities.sort(key=lambda o: (0 if o.confidence == "HIGH" else 1, -o.ev))
    return opportunities
