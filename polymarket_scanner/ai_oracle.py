"""AI Oracle — DeepSeek + Brave Search RAG probability estimator.

This module enriches Market objects with AI-generated true_prob estimates
by combining:
  1. Brave Search API  — fetches top-5 real-time news headlines as context
  2. DeepSeek Chat API — reasons over the news context to estimate probability

Environment Variables
---------------------
DEEPSEEK_API_KEY  (required) : Official DeepSeek API key
BRAVE_API_KEY     (optional) : Brave Search API key; if absent, falls back to
                               no-search mode (DeepSeek only, no live context)

Usage
-----
    from polymarket_scanner.ai_oracle import AIOracle
    from polymarket_scanner.models import Market

    oracle = AIOracle()
    enriched_market = oracle.enrich(market)   # returns Market with updated true_prob
    enriched_markets = oracle.enrich_all(markets)
"""

import json
import logging
import os
import time
from typing import List, Optional

import urllib.request
import urllib.parse
import urllib.error

from .models import Market

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEEPSEEK_API_URL  = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL    = "deepseek-chat"
BRAVE_SEARCH_URL  = "https://api.search.brave.com/res/v1/web/search"
BRAVE_MAX_RESULTS = 5
REQUEST_TIMEOUT   = 15   # seconds per HTTP call
RETRY_ATTEMPTS    = 2

# ---------------------------------------------------------------------------
# Brave Search helper
# ---------------------------------------------------------------------------
def _brave_search(query: str, api_key: str) -> List[dict]:
    """Call Brave Web Search API and return top-N result dicts.

    Each returned dict has keys: ``title``, ``description``.
    Returns an empty list on any error so callers can always fall back.
    """
    params = urllib.parse.urlencode({
        "q": query,
        "count": BRAVE_MAX_RESULTS,
        "text_decorations": False,
        "search_lang": "en",
    })
    url = f"{BRAVE_SEARCH_URL}?{params}"

    req = urllib.request.Request(
        url,
        headers={
            "Accept":               "application/json",
            "Accept-Encoding":      "gzip",
            "X-Subscription-Token": api_key,
        },
    )

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read()
                # urllib may return gzip-compressed bytes
                try:
                    import gzip as _gzip
                    raw = _gzip.decompress(raw)
                except Exception:
                    pass
                data = json.loads(raw.decode("utf-8"))

            results = []
            for item in data.get("web", {}).get("results", [])[:BRAVE_MAX_RESULTS]:
                results.append({
                    "title":       item.get("title", "").strip(),
                    "description": item.get("description", "").strip(),
                })
            return results

        except urllib.error.HTTPError as exc:
            logger.warning("Brave Search HTTP %s on attempt %d: %s", exc.code, attempt, exc.reason)
        except urllib.error.URLError as exc:
            logger.warning("Brave Search URL error on attempt %d: %s", attempt, exc.reason)
        except Exception as exc:
            logger.warning("Brave Search unexpected error on attempt %d: %s", attempt, exc)

        if attempt < RETRY_ATTEMPTS:
            time.sleep(1)

    return []

def _format_news_context(results: List[dict]) -> str:
    """Render search results as a numbered news-context block."""
    if not results:
        return "(No live search results available.)"
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "N/A")
        desc  = r.get("description", "")
        lines.append(f"{i}. {title}")
        if desc:
            lines.append(f"   {desc}")
    return "
".join(lines)

# ---------------------------------------------------------------------------
# DeepSeek API helper
# ---------------------------------------------------------------------------
def _deepseek_chat(messages: List[dict], api_key: str) -> str:
    """Send a chat request to DeepSeek and return the assistant content."""
    payload = json.dumps({
        "model":       DEEPSEEK_MODEL,
        "messages":    messages,
        "temperature": 0.2,
        "max_tokens":  256,
    }).encode("utf-8")

    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()

        except urllib.error.HTTPError as exc:
            logger.warning("DeepSeek HTTP %s on attempt %d: %s", exc.code, attempt, exc.reason)
        except urllib.error.URLError as exc:
            logger.warning("DeepSeek URL error on attempt %d: %s", attempt, exc.reason)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            logger.warning("DeepSeek response parse error on attempt %d: %s", attempt, exc)
        except Exception as exc:
            logger.warning("DeepSeek unexpected error on attempt %d: %s", attempt, exc)

        if attempt < RETRY_ATTEMPTS:
            time.sleep(1)

    raise RuntimeError("DeepSeek API call failed after all retry attempts.")

def _parse_probability(text: str) -> Optional[float]:
    """Extract the first float in [0, 1] from the model's response."""
    import re
    for pattern in (
        r"\b(0\.\d+|\.\d+|1\.0+|0|1)\b",   # bare float/int
        r"(\d{1,3})%",                        # percentage
    ):
        m = re.search(pattern, text)
        if m:
            val = float(m.group(1))
            if "%" in m.group(0):
                val /= 100.0
            if 0.0 <= val <= 1.0:
                return round(val, 4)
    return None

# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------
def _build_messages(market: Market, news_context: str) -> List[dict]:
    system_prompt = (
        "You are a probability calibration expert for prediction markets. "
        "Your task is to estimate the true probability of a binary market outcome "
        "resolving YES. Be concise and output ONLY a single number between 0 and 1 "
        "(e.g. 0.72). Do not include any other text."
    )

    user_content = f"""Market Question:
\"{market.question}\"

Current market price (implied probability): {market.price:.2%}
Category: {market.category}
Days to expiry: {market.days_to_expiry}

Latest News Context (from Brave Search):
{news_context}

Based on the news context above, what is your best estimate of the TRUE probability
that this market resolves YES? Reply with a single decimal number only (e.g. 0.68)."""

    return [
        {"role": "system",  "content": system_prompt},
        {"role": "user",    "content": user_content},
    ]

# ---------------------------------------------------------------------------
# Main Oracle class
# ---------------------------------------------------------------------------
class AIOracle:
    """Enriches Market objects with AI-estimated true_prob.

    Parameters
    ----------
    deepseek_api_key :
        DeepSeek API key.  Defaults to env var ``DEEPSEEK_API_KEY``.
    brave_api_key :
        Brave Search API key.  Defaults to env var ``BRAVE_API_KEY``.
        If neither is set, the oracle runs in no-search (fallback) mode.
    fallback_on_error :
        When True (default) any API error keeps the original market.true_prob.
        When False errors are re-raised.
    """

    def __init__(
        self,
        deepseek_api_key: Optional[str] = None,
        brave_api_key:    Optional[str] = None,
        fallback_on_error: bool = True,
    ):
        self.deepseek_key      = deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self.brave_key         = brave_api_key    or os.getenv("BRAVE_API_KEY", "")
        self.fallback_on_error = fallback_on_error

        if not self.deepseek_key:
            raise ValueError(
                "DeepSeek API key is required. "
                "Set the DEEPSEEK_API_KEY environment variable."
            )

        self._brave_enabled = bool(self.brave_key)
        if not self._brave_enabled:
            logger.info(
                "BRAVE_API_KEY not set — running in no-search fallback mode "
                "(DeepSeek only, no live news context)."
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_news_context(self, query: str) -> str:
        """Fetch news via Brave; return formatted context string."""
        if not self._brave_enabled:
            return "(Live search disabled — BRAVE_API_KEY not configured.)"
        results = _brave_search(query, self.brave_key)
        if not results:
            logger.warning("Brave Search returned no results for query: %r", query)
        return _format_news_context(results)

    def _estimate_prob(self, market: Market, news_context: str) -> float:
        """Call DeepSeek and parse the probability; raises on failure."""
        messages = _build_messages(market, news_context)
        response_text = _deepseek_chat(messages, self.deepseek_key)
        logger.debug("DeepSeek raw response for %r: %s", market.market_id, response_text)

        prob = _parse_probability(response_text)
        if prob is None:
            raise ValueError(
                f"Could not parse a probability from DeepSeek response: {response_text!r}"
            )
        return prob

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def enrich(self, market: Market) -> Market:
        """Return a copy of `market` with AI-updated true_prob.

        If ``fallback_on_error=True`` and any error occurs, the original
        market (with its existing true_prob) is returned unchanged.
        """
        try:
            news_context = self._get_news_context(market.question)
            new_prob     = self._estimate_prob(market, news_context)

            logger.info(
                "[%s] true_prob updated: %.4f → %.4f  (search: %s)",
                market.market_id,
                market.true_prob,
                new_prob,
                "yes" if self._brave_enabled else "no",
            )

            import dataclasses
            return dataclasses.replace(market, true_prob=new_prob)

        except Exception as exc:
            if self.fallback_on_error:
                logger.warning(
                    "[%s] AIOracle failed (%s), keeping original true_prob=%.4f",
                    market.market_id, exc, market.true_prob,
                )
                return market
            raise

    def enrich_all(self, markets: List[Market]) -> List[Market]:
        """Enrich an entire list of markets, returning enriched copies.

        Markets that fail enrichment are returned with their original
        true_prob (when fallback_on_error=True).
        """
        enriched = []
        for m in markets:
            enriched.append(self.enrich(m))
        return enriched
