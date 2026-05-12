"""Risk controller — Kelly Criterion position sizing + order-book execution advice.

Two responsibilities:
  1. POSITION SIZING via Quarter-Kelly
     ─────────────────────────────────
     Instead of a fixed 10% of capital, each opportunity gets a position
     size derived from the Kelly Criterion:

         f_raw = (true_prob - market_price) / (1 - market_price)   [Buy YES]
         f_raw = ((1-true_prob) - (1-market_price)) / market_price  [Buy NO]

     We apply Quarter-Kelly (f = f_raw * 0.25) and cap at
     max_kelly_position_ratio (default 20%) to prevent ruin.

     Kelly f can be negative (the formula tells you NOT to trade).
     Negative-Kelly opportunities are rejected.

  2. ORDER-BOOK EXECUTION ADVICE
     ────────────────────────────
     Using the market's best_bid and best_ask, we recommend:
     • TAKER  — if spread <= taker_spread_threshold (default 1¢): buy at ask
     • MAKER  — if spread >  taker_spread_threshold: place limit at bid+0.01

  3. TAKE PROFIT STAMPING (Phase 3)
     ─────────────────────────────
     After approval, RiskController stamps opp.take_profit_price:
         YES: TP = entry + (true_prob - entry) * tp_capture_ratio
         NO:  TP = entry - (entry - true_prob) * tp_capture_ratio
     Vol strategy uses target_price directly as the TP.
"""

from typing import List, Union

from .config import AccountConfig, DEFAULT_CONFIG
from .models import (
    OrderBookAdvice,
    RiskDecision,
    SmartMoneyOpportunity,
    StableOpportunity,
    VolatilityOpportunity,
)

Opportunity = Union[StableOpportunity, VolatilityOpportunity, SmartMoneyOpportunity]


# ---------------------------------------------------------------------------
# Kelly helpers
# ---------------------------------------------------------------------------

def _kelly_fraction(true_prob: float, market_price: float, side: str) -> float:
    """Compute raw (full) Kelly fraction for a binary Polymarket bet.

    Formula for Polymarket (binary, pays $1 on win):
        Buy YES: f = (p - P) / (1 - P)
        Buy NO:  f = ((1-p) - (1-P)) / (1 - (1-P))
                   = (P - p) / P

    Where p = true_prob, P = market_price.

    Returns the raw fraction (can be negative — caller should reject).
    """
    if side == "YES":
        denominator = 1.0 - market_price
        if abs(denominator) < 1e-9:
            return 0.0
        return (true_prob - market_price) / denominator
    else:  # NO
        denominator = market_price
        if abs(denominator) < 1e-9:
            return 0.0
        return ((1 - true_prob) - (1 - market_price)) / denominator


def _infer_side(opp: Opportunity) -> str:
    """Extract trade side from the opportunity's rationale list."""
    for note in opp.rationale:
        if "side=YES" in note:
            return "YES"
        if "side=NO" in note:
            return "NO"
        if "direction=BUY" in note or "(side=YES)" in note:
            return "YES"
        if "direction=SELL" in note or "(side=NO)" in note:
            return "NO"
    # Fallback: infer from EV sign vs price
    m = opp.market
    yes_ev = m.true_prob * (1 - m.price) - (1 - m.true_prob) * m.price
    no_ev  = (1 - m.true_prob) * m.price - m.true_prob * (1 - m.price)
    return "YES" if yes_ev >= no_ev else "NO"


# ---------------------------------------------------------------------------
# Take Profit helper (public so notifier can import it)
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
        TP  = 0.60 - 0.40 * 0.80 = 0.28
    """
    if side == "YES":
        tp = entry + (true_prob - entry) * ratio
    else:
        tp = entry - (entry - true_prob) * ratio
    return round(max(0.01, min(0.99, tp)), 4)


# ---------------------------------------------------------------------------
# Order-book advice
# ---------------------------------------------------------------------------

def _order_book_advice(market, side: str, cfg: AccountConfig) -> OrderBookAdvice:
    """Build execution advice based on the bid-ask spread.

    Logic:
    • If bid/ask are unavailable (both 0), fall back to market price.
    • If spread <= taker_spread_threshold → TAKER (pay the ask).
    • If spread >  taker_spread_threshold → MAKER (limit at bid + 0.01).
    """
    bid    = market.best_bid
    ask    = market.best_ask
    spread = market.spread  # uses the @property

    # Fall back gracefully when no order-book data is available
    if bid <= 0 or ask <= 0:
        return OrderBookAdvice(
            side=side,
            order_type="TAKER",
            limit_price=round(market.price, 4),
            spread=0.0,
            rationale="No order-book data — use market price as reference",
        )

    if spread <= cfg.taker_spread_threshold:
        return OrderBookAdvice(
            side=side,
            order_type="TAKER",
            limit_price=round(ask, 4),
            spread=round(spread, 4),
            rationale=(
                f"Tight spread ({spread:.4f} ≤ {cfg.taker_spread_threshold:.2f}) "
                f"→ buy at ask {ask:.4f}"
            ),
        )
    else:
        maker_price = round(bid + 0.01, 4)
        return OrderBookAdvice(
            side=side,
            order_type="MAKER",
            limit_price=maker_price,
            spread=round(spread, 4),
            rationale=(
                f"Wide spread ({spread:.4f} > {cfg.taker_spread_threshold:.2f}) "
                f"→ place limit at bid+0.01 = {maker_price:.4f} "
                f"(bid={bid:.4f}, ask={ask:.4f})"
            ),
        )


# ---------------------------------------------------------------------------
# RiskController
# ---------------------------------------------------------------------------

class RiskController:
    """Stateful: tracks cumulative volatility exposure."""

    def __init__(self, cfg: AccountConfig = DEFAULT_CONFIG):
        self.cfg = cfg
        self._volatility_sleeve_used: float = 0.0

    def reset(self) -> None:
        self._volatility_sleeve_used = 0.0

    def approve(self, opp: Opportunity) -> RiskDecision:
        cfg     = self.cfg
        reasons: List[str] = []
        m       = opp.market

        # ── Step 1: Kelly sizing ───────────────────────────────────────────
        side  = _infer_side(opp)
        f_raw = _kelly_fraction(m.true_prob, m.price, side)

        # Negative Kelly → the math says don't bet
        if f_raw <= 0:
            return RiskDecision(
                approved=False,
                reasons=[
                    f"Kelly f={f_raw:.4f} ≤ 0 "
                    f"(true_prob={m.true_prob:.4f}, price={m.price:.4f}, side={side})"
                ],
                kelly_f=round(f_raw, 4),
                kelly_position=0.0,
            )

        # Quarter-Kelly
        f_quarter = f_raw * cfg.kelly_fraction          # e.g. * 0.25
        kelly_pos  = f_quarter * cfg.total_capital

        # Hard cap at max_kelly_position_ratio
        cap_pos  = cfg.total_capital * cfg.max_kelly_position_ratio
        position = min(kelly_pos, cap_pos)
        if position < kelly_pos:
            reasons.append(
                f"Kelly position ${kelly_pos:.2f} capped to "
                f"${cap_pos:.2f} ({cfg.max_kelly_position_ratio:.0%} of capital)"
            )

        # ── Step 2: Absolute profit floor ─────────────────────────────────
        scale           = position / opp.suggested_position if opp.suggested_position > 0 else 0
        expected_profit = opp.expected_profit * scale

        if expected_profit < cfg.min_absolute_profit:
            return RiskDecision(
                approved=False,
                reasons=reasons + [
                    f"expected profit ${expected_profit:.2f} < "
                    f"${cfg.min_absolute_profit:.2f} floor"
                ],
                kelly_f=round(f_raw, 4),
                kelly_position=round(kelly_pos, 2),
            )

        # ── Step 3: Volatility sleeve cap ─────────────────────────────────
        if isinstance(opp, VolatilityOpportunity):
            sleeve_cap = cfg.total_capital * cfg.volatility_cap_ratio
            if self._volatility_sleeve_used + position > sleeve_cap:
                return RiskDecision(
                    approved=False,
                    reasons=reasons + [
                        f"volatility sleeve would exceed cap "
                        f"(${self._volatility_sleeve_used + position:.2f} > ${sleeve_cap:.2f})"
                    ],
                    kelly_f=round(f_raw, 4),
                    kelly_position=round(kelly_pos, 2),
                )
            self._volatility_sleeve_used += position

        # ── Step 4: Order-book advice ──────────────────────────────────────
        advice = _order_book_advice(m, side, cfg)

        # Stamp Kelly fields onto the opportunity object for formatter access
        opp.kelly_f        = round(f_raw, 4)
        opp.kelly_position = round(kelly_pos, 2)
        opp.order_advice   = advice

        # ── Step 5: Take Profit stamping (Phase 3) ─────────────────────────
        if isinstance(opp, VolatilityOpportunity):
            # Bracket target_price already defines the exit for vol trades
            opp.take_profit_price = opp.target_price
        else:
            opp.take_profit_price = compute_take_profit(
                entry     = m.price,
                true_prob = m.true_prob,
                side      = side,
                ratio     = cfg.tp_capture_ratio,
            )

        return RiskDecision(
            approved=True,
            reasons=reasons,
            approved_position=round(position, 2),
            kelly_f=round(f_raw, 4),
            kelly_position=round(kelly_pos, 2),
        )


# ---------------------------------------------------------------------------
# Functional wrapper (backwards-compat)
# ---------------------------------------------------------------------------

def risk_controller(
    opportunities: List[Opportunity],
    cfg: AccountConfig = DEFAULT_CONFIG,
) -> List[tuple]:
    """Returns [(opportunity, RiskDecision), ...]."""
    rc = RiskController(cfg)
    return [(opp, rc.approve(opp)) for opp in opportunities]
