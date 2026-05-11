"""Cross-platform arbitrage strategy — Polymarket vs Kalshi.

Thesis
------
Polymarket and Kalshi both offer binary (YES/NO) contracts on many of
the same real-world events.  When the markets are mis-priced relative
to each other, a risk-free arbitrage exists:

    Buy YES on platform A  +  Buy NO on platform B
    = guaranteed $1 payout regardless of outcome

The gross cost of this combined position is:
    combined_cost = poly_yes_price + kalshi_no_ask

If  combined_cost < 1.00  the spread is the guaranteed profit.

We apply a conservative 2 % slippage / fee buffer:
    THRESHOLD = 0.98

So the condition is:
    poly_yes_price + kalshi_no_ask  <  0.98   →  flag as arbitrage

Title matching
--------------
We use a simple keyword-overlap score to pair markets across platforms:
1. Tokenise both titles (lower-case, strip punctuation).
2. Compute Jaccard similarity of the token sets.
3. Accept the pair if similarity >= arb_min_title_similarity (default 0.30).

This is intentionally loose to catch paraphrased questions, at the cost
of occasional false positives — a human should still review each alert.
"""

import re
import string
from typing import Dict, List, Optional, Tuple

from ..config import AccountConfig, DEFAULT_CONFIG
from ..models import ArbitrageOpportunity, Market


# ---------------------------------------------------------------------------
# Title matching helpers
# ---------------------------------------------------------------------------
_STOPWORDS = {
    "will", "the", "a", "an", "in", "on", "by", "be", "to", "of",
    "and", "or", "is", "are", "was", "for", "at", "from", "with",
    "before", "after", "than", "that", "this", "it", "its",
}


def _tokenise(text: str) -> set:
    """Lower-case, strip punctuation, remove stopwords, return token set."""
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    tokens = set(text.split()) - _STOPWORDS
    # Also include bigrams for key phrases like "rate cut", "bitcoin price"
    words = [w for w in text.split() if w not in _STOPWORDS]
    bigrams = {f"{a}_{b}" for a, b in zip(words, words[1:])}
    return tokens | bigrams


def _jaccard(set_a: set, set_b: set) -> float:
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _title_similarity(poly_question: str, kalshi_title: str) -> float:
    return _jaccard(_tokenise(poly_question), _tokenise(kalshi_title))


# ---------------------------------------------------------------------------
# Arbitrage detection
# ---------------------------------------------------------------------------
def _find_best_kalshi_match(
    poly_market: Market,
    kalshi_markets: List[Dict],
    min_similarity: float,
) -> Optional[Tuple[Dict, float]]:
    """Return (kalshi_market_dict, similarity_score) or None."""
    best_score = 0.0
    best_match = None
    for km in kalshi_markets:
        score = _title_similarity(poly_market.question, km["title"])
        if score > best_score:
            best_score = score
            best_match = km
    if best_score >= min_similarity and best_match is not None:
        return best_match, best_score
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def arbitrage_strategy(
    poly_markets: List[Market],
    kalshi_markets: List[Dict],        # output of KalshiClient.fetch_active_markets()
    cfg: AccountConfig = DEFAULT_CONFIG,
) -> List[ArbitrageOpportunity]:
    """Scan for cross-platform arbitrage between Polymarket and Kalshi.

    Parameters
    ----------
    poly_markets   : Polymarket markets (standard Market dataclass list)
    kalshi_markets : raw dicts from KalshiClient (normalised form)
    cfg            : account config

    Returns
    -------
    List of ArbitrageOpportunity sorted by guaranteed profit descending.
    """
    opportunities: List[ArbitrageOpportunity] = []

    if not kalshi_markets:
        return opportunities

    max_position = cfg.total_capital * cfg.max_position_ratio

    for pm in poly_markets:
        # ── 1. Find a matching Kalshi market ─────────────────────────
        result = _find_best_kalshi_match(
            pm, kalshi_markets, cfg.arb_min_title_similarity
        )
        if result is None:
            continue
        km, sim_score = result

        # ── 2. Extract prices ─────────────────────────────────────────
        poly_yes   = pm.price                     # Polymarket YES price (bid/mid)
        kalshi_no  = km["no_ask"]                 # Kalshi NO ask price

        combined_cost = poly_yes + kalshi_no

        # ── 3. Check arbitrage condition ──────────────────────────────
        if combined_cost >= cfg.arb_threshold:
            continue

        # ── 4. Calculate guaranteed profit ────────────────────────────
        # Each $1 face-value position costs `combined_cost`,
        # pays $1 guaranteed → profit per $1 notional:
        profit_per_dollar = 1.0 - combined_cost

        # Position size limited by capital rules:
        # We size by expected profit ≥ min_absolute_profit
        # and position ≤ max_position_ratio * capital
        suggested_position = min(
            max_position,
            cfg.min_absolute_profit / profit_per_dollar if profit_per_dollar > 0 else 0,
        )
        # Snap up to at least the minimum that produces $0.50 profit
        # (risk controller enforces the hard floor later)
        suggested_position = min(max_position, max(suggested_position, max_position * 0.5))
        expected_profit = round(suggested_position * profit_per_dollar, 2)

        opportunities.append(
            ArbitrageOpportunity(
                poly_market=pm,
                kalshi_ticker=km["ticker"],
                kalshi_title=km["title"],
                title_similarity=round(sim_score, 3),
                poly_yes_price=round(poly_yes, 4),
                kalshi_no_price=round(kalshi_no, 4),
                combined_cost=round(combined_cost, 4),
                guaranteed_profit_pct=round(profit_per_dollar, 4),
                suggested_position=round(suggested_position, 2),
                expected_profit=expected_profit,
            )
        )

    # Best guaranteed profit first
    opportunities.sort(key=lambda o: o.guaranteed_profit_pct, reverse=True)
    return opportunities
