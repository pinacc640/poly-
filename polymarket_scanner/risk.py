"""Risk controller — position gating + Take Profit stamping.

Rules (applied in order)
------------------------
1. Single-position cap (soft clamp to max_position_ratio of capital)
2. Absolute profit floor  (>= min_absolute_profit USD)
3. Profit-ratio floor     (>= min_profit_ratio × capital)
4. Volatility sleeve cap  (<= volatility_cap_ratio of capital, aggregate)

Take Profit (Phase 3)
---------------------
After an opportunity passes all rules, RiskController stamps
`opp.take_profit_price` so the formatter and Telegram notifier can
display it without re-computing.

Formula:
    TP = entry + (true_prob - entry) * tp_capture_ratio   [YES side]
    TP = entry - (entry - true_prob) * tp_capture_ratio   [NO  side]

For VolatilityOpportunity the bracket target_price is already the TP.
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
# Take Profit helper (public so tests and notifier can import it)
# ---------------------------------------------------------------------------

def compute_take_profit(
    entry: float,
    true_prob: float,
    side: str,
    ratio: float,
) -> float:
    """Return the suggested Take Profit limit price.

    Captures `ratio` (default 0.80 = 80%) of the gap between the current
    market price (entry) and the AI's fair-value estimate (true_prob),
    rather than waiting for the contract to settle at 0 or 1.

    Examples
    --------
    entry=0.40, true_prob=0.80, side=YES, ratio=0.80
        gap = 0.80 - 0.40 = 0.40
        TP  = 0.40 + 0.40 * 0.80 = 0.72

    entry=0.60, true_prob=0.20, side=NO, ratio=0.80
        gap = 0.60 - 0.20 = 0.40   (YES price expected to fall)
        TP  = 0.60 - 0.40 * 0.80 = 0.28  (YES price at which we sell NO)
    """
    if side == "YES":
        tp = entry + (true_prob - entry) * ratio
    else:
        tp = entry - (entry - true_prob) * ratio
    return round(max(0.01, min(0.99, tp)), 4)


# ---------------------------------------------------------------------------
# RiskController
# ---------------------------------------------------------------------------

class RiskController:
    """Stateful: tracks cumulative volatility-sleeve usage."""

    def __init__(self, cfg: AccountConfig = DEFAULT_CONFIG):
        self.cfg = cfg
        self._volatility_sleeve_used: float = 0.0

    def reset(self) -> None:
        self._volatility_sleeve_used = 0.0

    def approve(self, opp: Opportunity) -> RiskDecision:
        cfg = self.cfg
        reasons: List[str] = []

        # ── Rule 1: single-position cap (soft clamp) ──────────────────────
        max_single = cfg.total_capital * cfg.max_position_ratio
        position = min(opp.suggested_position, max_single)
        if position < opp.suggested_position:
            reasons.append(
                f"clamped ${opp.suggested_position:.2f} → ${position:.2f}"
            )

        scale = position / opp.suggested_position if opp.suggested_position > 0 else 0
        expected_profit = opp.expected_profit * scale

        # ── Rule 2: absolute profit floor ─────────────────────────────────
        if expected_profit < cfg.min_absolute_profit:
            reasons.append(
                f"expected profit ${expected_profit:.2f} < "
                f"${cfg.min_absolute_profit:.2f} floor"
            )
            return RiskDecision(approved=False, reasons=reasons)

        # ── Rule 3: profit-ratio floor ─────────────────────────────────────
        min_profit_ratio_usd = cfg.total_capital * cfg.min_profit_ratio
        if expected_profit < min_profit_ratio_usd:
            reasons.append(
                f"expected profit ${expected_profit:.2f} < "
                f"{cfg.min_profit_ratio:.1%} of capital "
                f"(${min_profit_ratio_usd:.2f})"
            )
            return RiskDecision(approved=False, reasons=reasons)

        # ── Rule 4: volatility sleeve cap ─────────────────────────────────
        if isinstance(opp, VolatilityOpportunity):
            sleeve_cap = cfg.total_capital * cfg.volatility_cap_ratio
            if self._volatility_sleeve_used + position > sleeve_cap:
                reasons.append(
                    f"vol sleeve cap exceeded "
                    f"(${self._volatility_sleeve_used + position:.2f} > "
                    f"${sleeve_cap:.2f})"
                )
                return RiskDecision(approved=False, reasons=reasons)
            self._volatility_sleeve_used += position

        # ── Stamp Take Profit onto the opportunity ─────────────────────────
        if isinstance(opp, VolatilityOpportunity):
            # Bracket target is already the TP for vol trades
            opp.take_profit_price = opp.target_price
        else:
            opp.take_profit_price = compute_take_profit(
                entry     = opp.market.price,
                true_prob = opp.market.true_prob,
                side      = opp.side,
                ratio     = cfg.tp_capture_ratio,
            )

        return RiskDecision(
            approved=True,
            reasons=reasons,
            approved_position=round(position, 2),
        )


# ---------------------------------------------------------------------------
# Functional wrapper (backwards-compatible)
# ---------------------------------------------------------------------------

def risk_controller(
    opportunities: List[Opportunity],
    cfg: AccountConfig = DEFAULT_CONFIG,
) -> List[tuple]:
    """Returns [(opportunity, RiskDecision), ...]."""
    rc = RiskController(cfg)
    return [(opp, rc.approve(opp)) for opp in opportunities]
