"""Stable convergence strategy.

Thesis: near-expiry Polymarket contracts trading at extreme prices
(>= 0.80 or <= 0.20) on liquid, non-macro markets tend to converge
toward 0/1 with low variance. We only take trades where:

    - filters pass
    - composite score >= stable_min_score (default 5)
    - expected value is strictly positive
"""

from typing import List, Tuple

from ..config import AccountConfig, DEFAULT_CONFIG
from ..models import Market, StableOpportunity


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def _passes_filters(market: Market, cfg: AccountConfig) -> bool:
    if market.days_to_expiry > cfg.stable_max_days_to_expiry:
        return False
    if not (market.price >= cfg.stable_price_high or market.price <= cfg.stable_price_low):
        return False
    if market.liquidity < cfg.stable_min_liquidity:
        return False
    if market.category.lower() in cfg.macro_blocklist:
        return False
    return True


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _score(market: Market, cfg: AccountConfig) -> Tuple[int, List[str]]:
    score = 0
    notes: List[str] = []

    if market.days_to_expiry <= 7:
        score += 3
        notes.append("+3 expiry <=7d")

    if market.price >= 0.90 or market.price <= 0.10:
        score += 2
        notes.append("+2 extreme price")

    if market.volume_increasing:
        score += 2
        notes.append("+2 volume rising")

    if market.has_political_shock:
        score -= 3
        notes.append("-3 political shock")

    if market.category.lower() in cfg.macro_blocklist:
        # Filters should already have removed this, but scoring stays honest.
        score -= 5
        notes.append("-5 macro category")

    return score, notes


# ---------------------------------------------------------------------------
# Expected value
# ---------------------------------------------------------------------------
def _compute_ev(market: Market) -> Tuple[float, float, float, str]:
    """Return (ev_per_dollar, profit_space, loss_space, side).

    Side is "YES" when we're buying at high price expecting settle=1,
    "NO" when buying the NO side at (1-price) expecting settle=0.
    We always pick the side with non-negative true-prob edge.
    """
    p = market.price
    q = market.true_prob

    # YES side: pay p, win (1-p) if event happens, lose p otherwise.
    yes_profit_space = 1 - p
    yes_loss_space = p
    yes_ev = q * yes_profit_space - (1 - q) * yes_loss_space

    # NO side: pay (1-p), win p if event does NOT happen, lose (1-p) otherwise.
    no_profit_space = p
    no_loss_space = 1 - p
    no_ev = (1 - q) * no_profit_space - q * no_loss_space

    if yes_ev >= no_ev:
        return yes_ev, yes_profit_space, yes_loss_space, "YES"
    return no_ev, no_profit_space, no_loss_space, "NO"


# ---------------------------------------------------------------------------
# Risk label (purely informative; hard gating happens in risk_controller)
# ---------------------------------------------------------------------------
def _risk_level(score: int, ev_per_dollar: float) -> str:
    if score >= 8 and ev_per_dollar >= 0.05:
        return "Low"
    if score >= 6:
        return "Medium"
    return "High"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def stable_strategy(
    markets: List[Market],
    cfg: AccountConfig = DEFAULT_CONFIG,
) -> List[StableOpportunity]:
    """Scan `markets` and return stable-sleeve proposals.

    Proposals here are *candidates*; the risk controller has the final say.
    """
    opportunities: List[StableOpportunity] = []
    max_position = cfg.total_capital * cfg.max_position_ratio

    for m in markets:
        if not _passes_filters(m, cfg):
            continue

        score, notes = _score(m, cfg)
        if score < cfg.stable_min_score:
            continue

        ev_per_dollar, profit_space, loss_space, side = _compute_ev(m)
        if ev_per_dollar <= 0:
            continue

        # Profit per $1 of notional (not per $1 at risk):
        # a full $1 of notional pays `profit_space` if the true_prob
        # materializes, so expected profit in USD is:
        expected_profit = max_position * (
            m.true_prob * profit_space - (1 - m.true_prob) * loss_space
        ) if side == "YES" else max_position * (
            (1 - m.true_prob) * profit_space - m.true_prob * loss_space
        )

        notes.insert(0, f"side={side}")
        opportunities.append(
            StableOpportunity(
                market=m,
                score=score,
                ev=round(ev_per_dollar, 4),
                suggested_position=round(max_position, 2),
                expected_profit=round(expected_profit, 2),
                risk_level=_risk_level(score, ev_per_dollar),
                rationale=notes,
            )
        )

    # Best opportunities first
    opportunities.sort(key=lambda o: (o.score, o.ev), reverse=True)
    return opportunities
