"""Mapper: Gamma API raw dict  →  Market dataclass.

Design goals
------------
- Every field access is defensive: missing / null / bad-typed values
  fall back to a safe default and emit a debug log rather than raising.
- The mapper is a pure function — no I/O, easy to unit-test.
- Category detection uses the event's tag slugs, falling back to a
  keyword scan of the question text.

Gamma API → Market field mapping
---------------------------------
Gamma field            | Market field          | Notes
-----------------------|-----------------------|----------------------------
id                     | market_id             |
question               | question              |
tag slugs (event)      | category              | see _infer_category()
outcomePrices[0]       | price                 | YES price, 0..1
liquidityNum           | liquidity             | USD
volume24hr             | volume_24h            | USD
volume24hr (prev est.) | volume_prev_24h       | estimated from 1wk avg
oneDayPriceChange      | price_change_24h      |
endDate                | days_to_expiry        | derived
price                  | true_prob             | proxy = mid-point price
has_political_shock    | False                 | no live signal available
has_fundamental_change | False                 | no live signal available
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import Market

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category detection
# ---------------------------------------------------------------------------

# Maps Gamma tag slugs → our internal category strings.
# Order matters: first match wins.
_TAG_SLUG_MAP: List[tuple] = [
    # Macro / commodity — blocklisted by stable strategy
    ("oil",           "oil"),
    ("commodity",     "oil"),
    ("gold",          "gold"),
    ("crypto",        "crypto"),
    ("bitcoin",       "crypto"),
    ("ethereum",      "crypto"),
    # Geopolitics / war
    ("politics",      "politics"),
    ("geopolitics",   "geopolitics"),
    ("war",           "war"),
    ("elections",     "politics"),
    ("election",      "politics"),
    # Sports
    ("sports",        "sports"),
    ("soccer",        "sports"),
    ("nfl",           "sports"),
    ("nba",           "sports"),
    ("mlb",           "sports"),
    ("tennis",        "sports"),
    ("mma",           "sports"),
    # Entertainment / pop-culture
    ("pop-culture",   "entertainment"),
    ("entertainment", "entertainment"),
    ("awards",        "entertainment"),
    # Science / tech
    ("science",       "science"),
    ("technology",    "tech"),
    ("ai",            "tech"),
    # Business / finance
    ("business",      "business"),
    ("finance",       "business"),
    ("economics",     "macro"),
    ("economy",       "macro"),
]

# Keyword fallback scan on question text (lower-cased).
_QUESTION_KEYWORD_MAP: List[tuple] = [
    ("bitcoin",    "crypto"),
    ("btc",        "crypto"),
    ("ethereum",   "crypto"),
    ("eth ",       "crypto"),
    ("crypto",     "crypto"),
    ("oil",        "oil"),
    ("crude",      "oil"),
    ("gold",       "gold"),
    ("silver",     "gold"),
    ("war",        "war"),
    ("military",   "war"),
    ("geopolit",   "geopolitics"),
    ("election",   "politics"),
    ("president",  "politics"),
    ("senate",     "politics"),
    ("congress",   "politics"),
    ("fed ",       "macro"),
    ("fomc",       "macro"),
    ("rate cut",   "macro"),
    ("gdp",        "macro"),
    ("inflation",  "macro"),
]


def _infer_category(raw: Dict[str, Any]) -> str:
    """Infer a category string from event tags, then question text."""
    # 1. Try event tags
    events: List[Dict] = raw.get("events") or []
    for event in events:
        for tag in event.get("tags") or []:
            slug = (tag.get("slug") or "").lower()
            for pattern, category in _TAG_SLUG_MAP:
                if pattern in slug:
                    return category

    # 2. Keyword scan on question
    question = (raw.get("question") or "").lower()
    for keyword, category in _QUESTION_KEYWORD_MAP:
        if keyword in question:
            return category

    return "other"


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_yes_price(raw: Dict[str, Any]) -> float:
    """Extract YES price from outcomePrices (JSON-encoded string or list)."""
    op = raw.get("outcomePrices")
    if op is None:
        # Fall back to lastTradePrice
        return _safe_float(raw.get("lastTradePrice"), 0.5)
    try:
        if isinstance(op, str):
            prices = json.loads(op)
        else:
            prices = op
        return _safe_float(prices[0], 0.5)
    except (json.JSONDecodeError, IndexError, TypeError):
        log.debug("Could not parse outcomePrices=%r, using lastTradePrice", op)
        return _safe_float(raw.get("lastTradePrice"), 0.5)


def _parse_bid_ask(raw: Dict[str, Any], yes_price: float) -> tuple:
    """Extract best bid and best ask from Gamma API response.

    Gamma API field candidates:
      - bestBid / bestAsk  (some endpoints)
      - bestBidPrice / bestAskPrice
      - spread (if available, used to synthesise bid/ask around mid)

    If explicit bid/ask fields are absent, we synthesise a conservative
    estimate:  bid = price - half_tick,  ask = price + half_tick,
    where half_tick = 0.01 (the minimum Polymarket tick size).
    """
    # Try explicit fields first
    bid = _safe_float(raw.get("bestBid") or raw.get("bestBidPrice"), 0.0)
    ask = _safe_float(raw.get("bestAsk") or raw.get("bestAskPrice"), 0.0)

    if bid > 0 and ask > 0 and ask > bid:
        return round(bid, 4), round(ask, 4)

    # Synthesise from spread field if available
    spread = _safe_float(raw.get("spread"), 0.0)
    if spread > 0:
        half = spread / 2.0
        return round(yes_price - half, 4), round(yes_price + half, 4)

    # Last resort: use ±1 tick around the mid-price
    tick = 0.01
    return round(max(0.01, yes_price - tick), 4), round(min(0.99, yes_price + tick), 4)


def _days_to_expiry(raw: Dict[str, Any]) -> int:
    """Compute calendar days from now to endDate (floor, minimum 0)."""
    end_str: Optional[str] = raw.get("endDate") or raw.get("endDateIso")
    if not end_str:
        return 999   # Unknown → treat as far future (filters will exclude)
    try:
        # Handle both ISO datetime and date strings
        end_str_clean = end_str.rstrip("Z").split(".")[0]
        if "T" in end_str_clean:
            end_dt = datetime.fromisoformat(end_str_clean).replace(tzinfo=timezone.utc)
        else:
            end_dt = datetime.fromisoformat(end_str_clean).replace(
                hour=23, minute=59, tzinfo=timezone.utc
            )
        now = datetime.now(tz=timezone.utc)
        delta = (end_dt - now).total_seconds() / 86_400
        return max(0, int(delta))
    except ValueError:
        log.debug("Could not parse endDate=%r", end_str)
        return 999


def _estimate_prev_volume(raw: Dict[str, Any]) -> float:
    """Estimate previous 24h volume from the 1-week figure.

    We don't have yesterday's 24h volume directly, so we approximate
    it as the 1-week average.  If the market is trending up, today's
    volume will exceed this average (volume_increasing = True), which
    is the right direction for our scorer.
    """
    v24 = _safe_float(raw.get("volume24hr"), 0.0)
    v1wk = _safe_float(raw.get("volume1wk") or raw.get("volume1wkClob"), 0.0)
    if v1wk > 0:
        return v1wk / 7.0
    # If no weekly data, assume flat volume
    return v24


# ---------------------------------------------------------------------------
# Public mapper
# ---------------------------------------------------------------------------

def map_to_market(raw: Dict[str, Any]) -> Optional[Market]:
    """Convert a single Gamma API market dict to a Market dataclass.

    Returns None if the record is missing essential fields (price,
    question, id) — the caller should silently skip these.
    """
    market_id = str(raw.get("id") or raw.get("slug") or "")
    question   = str(raw.get("question") or "").strip()

    if not market_id or not question:
        log.debug("Skipping record with missing id/question: %r", raw)
        return None

    price = _parse_yes_price(raw)
    # Clamp to valid probability range — API occasionally returns 0 or 1 exactly
    price = max(0.001, min(0.999, price))

    # We use market price as our best available estimate of true probability.
    # In a production system you'd source this from a separate model or oracle.
    true_prob = price

    category = _infer_category(raw)

    liquidity       = _safe_float(raw.get("liquidityNum") or raw.get("liquidity"), 0.0)
    volume_24h      = _safe_float(raw.get("volume24hr") or raw.get("volume24hrClob"), 0.0)
    volume_prev_24h = _estimate_prev_volume(raw)
    price_change    = _safe_float(raw.get("oneDayPriceChange"), 0.0)
    days            = _days_to_expiry(raw)

    best_bid, best_ask = _parse_bid_ask(raw, price)

    return Market(
        market_id=market_id,
        question=question,
        category=category,
        price=price,
        liquidity=liquidity,
        volume_24h=volume_24h,
        volume_prev_24h=volume_prev_24h,
        price_change_24h=price_change,
        days_to_expiry=days,
        true_prob=true_prob,
        has_political_shock=False,       # no live signal; override manually if needed
        has_fundamental_change=False,    # no live signal; override manually if needed
        best_bid=best_bid,
        best_ask=best_ask,
    )


def map_markets(raw_list: List[Dict[str, Any]]) -> List[Market]:
    """Map a list of raw API dicts; skip any that fail validation."""
    result: List[Market] = []
    for raw in raw_list:
        m = map_to_market(raw)
        if m is not None:
            result.append(m)
    return result
