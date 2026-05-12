"""Smart Money 2.0 — Whale detection with price-impact filter.

Phase 3 upgrade: adds price_impact_ratio and is_breakout to every
SmartMoneyOpportunity. Markets with high volume but no real price
movement (wash trading / fake volume) are rejected.

price_impact_ratio = abs(price_change_24h) / (volume_24h / liquidity)

Intuition: a real whale pouring $500k into a $1M book (ratio=0.50)
that only moves price 0.1% → impact ratio = 0.002 → wash trading.
Real conviction should move price substantially per unit of pressure.

Confidence levels
-----------------
HIGH   : vol spike  +  price impact passes  +  breakout confirmed
MEDIUM : vol spike  +  price impact passes  (no full breakout yet)
LOW    : vol spike only  →  DROPPED (wash-trading trap)
"""

from typing import List

from ..config import AccountConfig, DEFAULT_CONFIG
from ..models import Market, SmartMoneyOpportunity


# ---------------------------------------------------------------------------
# Pre-filters
# ---------------------------------------------------------------------------

def _passes_basic_filters(m: Market, cfg: AccountConfig) -> bool:
    if m.volume_24h < cfg.sm_min_volume_24h:
        return False
    if m.liquidity < cfg.sm_min_liquidity:
        return False
    if m.days_to_expiry > cfg.sm_max_days_to_expiry:
        return False
    return True


# ---------------------------------------------------------------------------
# Price-impact ratio (whale filter)
# ---------------------------------------------------------------------------

def _price_impact_ratio(m: Market) -> float:
    """Compute price_impact_ratio = abs(Δprice) / (volume / liquidity)."""
    if m.liquidity <= 0:
        return 0.0
    vol_pressure = m.volume_24h / m.liquidity
    if vol_pressure <= 0:
        return 0.0
    return abs(m.price_change_24h) / vol_pressure


# ---------------------------------------------------------------------------
# Confidence classification
# ---------------------------------------------------------------------------

def _classify(
    m: Market,
    cfg: AccountConfig,
    vol_liq_ratio: float,
    price_impact: float,
) -> str:
    has_vol_spike    = vol_liq_ratio >= cfg.sm_min_vol_liq_ratio
    has_price_impact = price_impact  >= cfg.sm_min_price_impact_ratio
    has_breakout     = abs(m.price_change_24h) >= cfg.sm_breakout_threshold
    has_big_move     = abs(m.price_change_24h) >= cfg.sm_min_price_move

    if has_vol_spike and has_price_impact and has_breakout and has_big_move:
        return "HIGH"
    if has_vol_spike and has_price_impact:
        return "MEDIUM"
    if has_vol_spike:
        return "LOW"   # wash-trading candidate — dropped downstream
    return "NONE"


# ---------------------------------------------------------------------------
# EV calculation
# ---------------------------------------------------------------------------

def _compute_ev(m: Market, side: str, confidence: str, cfg: AccountConfig) -> float:
    edge = (
        cfg.sm_high_confidence_edge if confidence == "HIGH"
        else cfg.sm_medium_confidence_edge
    )
    if side == "YES":
        adj_prob = min(0.99, m.true_prob + edge)
        return adj_prob * (1 - m.price) - (1 - adj_prob) * m.price
    else:
        adj_prob = max(0.01, m.true_prob - edge)
        no_price = 1 - m.price
        return (1 - adj_prob) * no_price - adj_prob * (1 - no_price)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def smart_money_strategy(
    markets: List[Market],
    cfg: AccountConfig = DEFAULT_CONFIG,
) -> List[SmartMoneyOpportunity]:
    """Return HIGH and MEDIUM confidence whale-flow opportunities.

    LOW / NONE confidence are silently dropped to avoid wash-trading traps.
    """
    opps: List[SmartMoneyOpportunity] = []
    max_position = cfg.total_capital * cfg.max_position_ratio

    for m in markets:
        if not _passes_basic_filters(m, cfg):
            continue

        vol_liq_ratio = m.volume_24h / m.liquidity if m.liquidity > 0 else 0.0
        price_impact  = _price_impact_ratio(m)
        confidence    = _classify(m, cfg, vol_liq_ratio, price_impact)

        if confidence not in ("HIGH", "MEDIUM"):
            continue   # LOW = wash trading; NONE = no signal

        # Flow direction from price movement sign
        if m.price_change_24h >= 0:
            flow_direction = "BUY"
            side           = "YES"
        else:
            flow_direction = "SELL"
            side           = "NO"

        ev = _compute_ev(m, side, confidence, cfg)
        if ev <= 0:
            continue

        is_breakout     = abs(m.price_change_24h) >= cfg.sm_breakout_threshold
        expected_profit = max_position * ev

        notes = [
            f"side={side}",
            f"flow={flow_direction}",
            f"vol/liq={vol_liq_ratio:.2f}",
            f"price_impact={price_impact:.3f}",
            f"breakout={'YES' if is_breakout else 'NO'}",
            f"Δ24h={m.price_change_24h:+.2%}",
            f"volume=${m.volume_24h:,.0f}",
        ]
        if not is_breakout:
            notes.append("⚠ no breakout — consider smaller size")

        opps.append(
            SmartMoneyOpportunity(
                market             = m,
                confidence         = confidence,
                flow_direction     = flow_direction,
                volume_spike_ratio = round(vol_liq_ratio, 3),
                price_move_pct     = round(abs(m.price_change_24h), 4),
                ev                 = round(ev, 4),
                suggested_position = round(max_position, 2),
                expected_profit    = round(expected_profit, 2),
                price_impact_ratio = round(price_impact, 4),
                is_breakout        = is_breakout,
                rationale          = notes,
            )
        )

    # HIGH first, then by EV descending
    opps.sort(key=lambda o: (0 if o.confidence == "HIGH" else 1, -o.ev))
    return opps
