"""Smart Money accumulation strategy.

Thesis
------
When large, informed players ("smart money") take a position on
Polymarket, two footprints appear simultaneously:

  1. **Volume spike** — 24h volume is unusually high relative to the
     resting liquidity (volume / liquidity ratio >> 1).  Passive
     liquidity providers don't create this; aggressive order flow does.

  2. **Directional price move** — the price drifts strongly in one
     direction (>= sm_min_price_move) while volume is elevated,
     indicating that the flow is *one-sided*, not two-way noise.

  3. **Raw volume threshold** — absolute 24h volume >= sm_min_volume_24h
     rules out thin markets where a single retail trade looks like a
     spike.

When all three signals fire we assign HIGH confidence and apply a
larger EV edge (sm_high_confidence_edge).  When only the volume +
price-move signals fire we assign MEDIUM.  Volume alone gives LOW.

The EV edge is the key modelling assumption: we trust that the entity
moving price has better information than the market consensus, so we
adjust true_prob upward (for a BUY signal) or downward (for a SELL).

Limitations / known caveats
----------------------------
- We cannot distinguish genuine informed flow from a single whale
  who is simply wrong.
- Price moves near expiry can look like smart-money even for
  trivially settling markets — the stable_strategy is better suited
  for those.
- This strategy should be used as a *filter* to surface candidates
  for human review, not as a fully autonomous signal.
"""

from typing import List, Literal, Tuple

from ..config import AccountConfig, DEFAULT_CONFIG
from ..models import Market, SmartMoneyOpportunity


# ---------------------------------------------------------------------------
# Signal detection helpers
# ---------------------------------------------------------------------------

def _vol_liq_ratio(market: Market) -> float:
    """volume_24h / liquidity.  Returns 0 if liquidity is zero."""
    if market.liquidity <= 0:
        return 0.0
    return market.volume_24h / market.liquidity


def _flow_direction(market: Market) -> Literal["BUY", "SELL"]:
    """Infer direction from the sign of the 24h price change.

    A rising price means buyers are driving it → BUY signal.
    A falling price means sellers are pushing it down → SELL signal.
    """
    return "BUY" if market.price_change_24h >= 0 else "SELL"


def _confidence(
    market: Market,
    cfg: AccountConfig,
) -> Tuple[Literal["HIGH", "MEDIUM", "LOW", "NONE"], List[str]]:
    """Return (confidence_level, signal_notes).

    NONE means the market does not qualify at all.
    """
    signals: List[str] = []
    n_signals = 0

    # Signal 1 — raw volume threshold
    has_volume = market.volume_24h >= cfg.sm_min_volume_24h
    if has_volume:
        n_signals += 1
        signals.append(f"vol24h=${market.volume_24h:,.0f} ≥ ${cfg.sm_min_volume_24h:,.0f}")

    # Signal 2 — directional price move
    has_move = abs(market.price_change_24h) >= cfg.sm_min_price_move
    if has_move:
        n_signals += 1
        signals.append(f"price_move={market.price_change_24h:+.1%} ≥ {cfg.sm_min_price_move:.0%}")

    # Signal 3 — vol/liq ratio (aggressive book crossing)
    ratio = _vol_liq_ratio(market)
    has_ratio = ratio >= cfg.sm_min_vol_liq_ratio
    if has_ratio:
        n_signals += 1
        signals.append(f"vol/liq={ratio:.2f} ≥ {cfg.sm_min_vol_liq_ratio:.2f}")

    # Must have at least the volume signal to qualify at all
    if not has_volume:
        return "NONE", signals

    if n_signals == 3:
        return "HIGH", signals
    if has_volume and has_move:
        return "MEDIUM", signals
    if has_volume:
        return "LOW", signals

    return "NONE", signals


# ---------------------------------------------------------------------------
# EV calculation
# ---------------------------------------------------------------------------

def _adjusted_true_prob(
    market: Market,
    direction: Literal["BUY", "SELL"],
    confidence: Literal["HIGH", "MEDIUM", "LOW"],
    cfg: AccountConfig,
) -> float:
    """Return a true_prob adjusted upward/downward by the confidence edge.

    BUY signal  → smart money thinks YES is more likely → boost true_prob
    SELL signal → smart money thinks NO  is more likely → reduce true_prob
    """
    edge = {
        "HIGH":   cfg.sm_high_confidence_edge,
        "MEDIUM": cfg.sm_medium_confidence_edge,
        "LOW":    0.0,
    }[confidence]

    if direction == "BUY":
        return min(0.999, market.true_prob + edge)
    else:
        return max(0.001, market.true_prob - edge)


def _compute_ev(
    market: Market,
    adj_prob: float,
    direction: Literal["BUY", "SELL"],
) -> Tuple[float, Literal["YES", "NO"]]:
    """EV per $1 of notional, following the smart-money direction.

    BUY  → we buy YES tokens (pay price, win 1-price)
    SELL → we buy NO  tokens (pay 1-price, win price)
    """
    p = market.price
    q = adj_prob

    if direction == "BUY":
        # Pay p per share, collect $1 if resolves YES
        ev = q * (1 - p) - (1 - q) * p
        return ev, "YES"
    else:
        # Pay (1-p) per share, collect $1 if resolves NO
        ev = (1 - q) * p - q * (1 - p)
        return ev, "NO"


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _passes_filters(market: Market, cfg: AccountConfig) -> bool:
    if market.liquidity < cfg.sm_min_liquidity:
        return False
    if market.days_to_expiry > cfg.sm_max_days_to_expiry:
        return False
    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def smart_money_strategy(
    markets: List[Market],
    cfg: AccountConfig = DEFAULT_CONFIG,
) -> List[SmartMoneyOpportunity]:
    """Scan `markets` for smart-money accumulation signals.

    Returns a list of SmartMoneyOpportunity objects sorted by
    confidence (HIGH first) then EV descending.

    Proposals here are *candidates*; the risk controller has the final say.
    """
    opportunities: List[SmartMoneyOpportunity] = []
    max_position = cfg.total_capital * cfg.max_position_ratio

    for m in markets:
        if not _passes_filters(m, cfg):
            continue

        confidence, signal_notes = _confidence(m, cfg)
        if confidence == "NONE":
            continue

        direction = _flow_direction(m)
        adj_prob  = _adjusted_true_prob(m, direction, confidence, cfg)
        ev, side  = _compute_ev(m, adj_prob, direction)

        # Only surface positive-EV opportunities
        if ev <= 0:
            continue

        ratio           = _vol_liq_ratio(m)
        expected_profit = max_position * ev

        rationale = [
            f"confidence={confidence}",
            f"direction={direction} (side={side})",
            f"adj_prob={adj_prob:.3f} (raw={m.true_prob:.3f})",
        ] + signal_notes

        opportunities.append(
            SmartMoneyOpportunity(
                market=m,
                confidence=confidence,
                volume_spike_ratio=round(ratio, 3),
                price_move_pct=round(abs(m.price_change_24h), 4),
                flow_direction=direction,
                ev=round(ev, 4),
                suggested_position=round(max_position, 2),
                expected_profit=round(expected_profit, 2),
                rationale=rationale,
            )
        )

    # Sort: HIGH before MEDIUM before LOW, then by EV descending
    _order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    opportunities.sort(key=lambda o: (_order[o.confidence], -o.ev))
    return opportunities
