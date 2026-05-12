"""Risk controller.

The controller is the last line of defense: strategies propose,
the controller disposes. It enforces hard numeric rules regardless
of strategy enthusiasm:

    1.  single position  <= max_position_ratio of total_capital  (soft clamp)
    2.  expected profit  >= min_absolute_profit                  (hard reject)
    3.  total volatility sleeve <= volatility_cap_ratio           (hard reject)

Additionally computes and stamps take_profit_price on every approved
opportunity so the formatter and notifier can surface it without
re-deriving the value.

Take Profit formula
-------------------
For YES trades:  TP = entry + (true_prob - entry) * tp_capture_ratio
For NO trades:   TP = entry - ((1 - true_prob) - (1 - entry)) * tp_capture_ratio
                    = entry - (entry - true_prob) * tp_capture_ratio

Simplified: capture `tp_capture_ratio` (default 80%) of the gap between
current market price and AI fair value, rather than waiting for settlement.
"""

from typing import List, Union

from .config import AccountConfig, DEFAULT_CONFIG
from .models import (
    RiskDecision,
    SmartMoneyOpportunity,
    StableOpportunity,
    VolatilityOpportunity,
)

Opportunity = Union[StableOpportunity, VolatilityOpportunity, SmartMoneyOpportunity]


# ---------------------------------------------------------------------------
# Take Profit calculation
# ---------------------------------------------------------------------------

def compute_take_profit(entry: float, true_prob: float, side: str, ratio: float) -> float:
    """Compute suggested Take Profit limit price.

    Args:
        entry:     current market price (what you pay to enter)
        true_prob: AI-estimated fair probability
        side:      "YES" or "NO"
        ratio:     fraction of the edge gap to capture (e.g. 0.80)

    Returns:
        Suggested TP price, clamped to [0.01, 0.99].

    Examples:
        entry=0.40, true_prob=0.80, side=YES, ratio=0.80
        gap = 0.80 - 0.40 = 0.40
        TP  = 0.40 + 0.40 * 0.80 = 0.72

        entry=0.60, true_prob=0.20, side=NO (buying NO at 1-0.60=0.40)
        For NO we think YES will NOT happen, so YES price will fall.
        gap = 0.60 - 0.20 = 0.40  (price will drop toward true_prob)
        TP  = 0.60 - 0.40 * 0.80 = 0.28  (YES token price target; sell NO at 1-0.28=0.72)
    """
    if side == "YES":
        gap = true_prob - entry
        tp = entry + gap * ratio
    else:
        # NO side: we want YES price to drop from entry toward true_prob
        gap = entry - true_prob
        tp = entry - gap * ratio   # this is the YES price at TP; reported as YES price

    return round(max(0.01, min(0.99, tp)), 4)


# ---------------------------------------------------------------------------
# RiskController
# ---------------------------------------------------------------------------

class RiskController:
    """Stateful so it can track cumulative volatility exposure."""

    def __init__(self, cfg: AccountConfig = DEFAULT_CONFIG):
        self.cfg = cfg
        self._volatility_sleeve_used: float = 0.0

    def reset(self) -> None:
        self._volatility_sleeve_used = 0.0

    def approve(self, opp: Opportunity) -> RiskDecision:
        cfg = self.cfg
        reasons: List[str] = []

        # --- Rule 1: single-position cap (soft: clamp) ---
        max_single = cfg.total_capital * cfg.max_position_ratio
        position = min(opp.suggested_position, max_single)
        if position < opp.suggested_position:
            reasons.append(
                f"clamped position ${opp.suggested_position:.2f} → ${position:.2f}"
            )

        # Re-scale expected profit proportionally to clamped size
        scale = position / opp.suggested_position if opp.suggested_position > 0 else 0
        expected_profit = opp.expected_profit * scale

        # --- Rule 2: absolute profit floor ---
        if expected_profit < cfg.min_absolute_profit:
            reasons.append(
                f"expected profit ${expected_profit:.2f} < ${cfg.min_absolute_profit:.2f} floor"
            )
            return RiskDecision(approved=False, reasons=reasons)

        # --- Rule 3: volatility sleeve cap ---
        if isinstance(opp, VolatilityOpportunity):
            sleeve_cap = cfg.total_capital * cfg.volatility_cap_ratio
            if self._volatility_sleeve_used + position > sleeve_cap:
                reasons.append(
                    f"volatility sleeve would exceed cap "
                    f"(${self._volatility_sleeve_used + position:.2f} > ${sleeve_cap:.2f})"
                )
                return RiskDecision(approved=False, reasons=reasons)
            self._volatility_sleeve_used += position

        # --- Stamp Take Profit price onto the opportunity ---
        entry = opp.market.price
        side  = getattr(opp, "side", "YES")

        if isinstance(opp, VolatilityOpportunity):
            # For volatility, TP = the bracket target (already calculated)
            opp.take_profit_price = opp.target_price
        else:
            opp.take_profit_price = compute_take_profit(
                entry     = entry,
                true_prob = opp.market.true_prob,
                side      = side,
                ratio     = cfg.tp_capture_ratio,
            )

        return RiskDecision(
            approved=True,
            reasons=reasons,
            approved_position=round(position, 2),
        )


def risk_controller(
    opportunities: List[Opportunity],
    cfg: AccountConfig = DEFAULT_CONFIG,
) -> List[tuple]:
    """Functional wrapper: returns [(opportunity, RiskDecision), ...]."""
    rc = RiskController(cfg)
    return [(opp, rc.approve(opp)) for opp in opportunities]
