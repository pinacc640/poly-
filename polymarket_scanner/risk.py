"""Risk controller.

The controller is the last line of defense: strategies propose,
the controller disposes. It enforces hard numeric rules regardless
of strategy enthusiasm:

    1.  single position  <= 10% of total_capital
    2.  expected profit  >= $0.50  (absolute floor)
    3.  total volatility sleeve <= 20% of total_capital

Rule 1 is enforced by *downsizing* the position (soft). Rules 2-3
are hard rejections.

NOTE: The 1%-of-capital profit ratio floor (former Rule 3) has been
removed.  It was redundant with the $0.50 absolute floor for small
accounts and became over-restrictive as capital grew.
"""

from typing import List, Union

from .config import AccountConfig, DEFAULT_CONFIG
from .models import RiskDecision, StableOpportunity, VolatilityOpportunity


Opportunity = Union[StableOpportunity, VolatilityOpportunity]


class RiskController:
    """Stateful so it can track cumulative volatility exposure."""

    def __init__(self, cfg: AccountConfig = DEFAULT_CONFIG):
        self.cfg = cfg
        self._volatility_sleeve_used: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
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
                f"clamped position ${opp.suggested_position:.2f} -> ${position:.2f}"
            )

        # Re-scale expected profit proportionally to clamped size.
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
            # Reserve the capacity
            self._volatility_sleeve_used += position

        return RiskDecision(
            approved=True,
            reasons=reasons,
            approved_position=round(position, 2),
        )


def risk_controller(
    opportunities: List[Opportunity],
    cfg: AccountConfig = DEFAULT_CONFIG,
) -> List[tuple]:
    """Functional wrapper: returns [(opportunity, RiskDecision), ...].

    Preserves input order so callers can decide what to do with
    rejected proposals (e.g. log, display as warnings).
    """
    rc = RiskController(cfg)
    return [(opp, rc.approve(opp)) for opp in opportunities]
