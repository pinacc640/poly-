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
import re
import time
from typing import List, Optional, Tuple

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
def _brave_search(
    query: str,
    api_key: str,
    max_results: int = BRAVE_MAX_RESULTS,
    timeout: int = REQUEST_TIMEOUT,
) -> List[dict]:
    """Call Brave Web Search API and return top-N result dicts.

    Each returned dict has keys: ``title``, ``description``.
    Returns an empty list on any error so callers can always fall back.
    """
    params = urllib.parse.urlencode({
        "q": query,
        "count": max_results,
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
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    import gzip as _gzip
                    raw = _gzip.decompress(raw)
                except Exception:
                    pass
                data = json.loads(raw.decode("utf-8"))

            results = []
            for item in data.get("web", {}).get("results", [])[:max_results]:
                results.append({
                    "title":       item.get("title", "").strip(),
                    "description": item.get("description", "").strip(),
                })
            return results

        except urllib.error.HTTPError as exc:
            logger.warning("Brave Search HTTP %s on attempt %d: %s", exc.code, attempt, exc.reason)
        except urllib.error.URLError as exc:
            logger.warning("Brave Search URL error on attempt %d: %s", attempt, exc.reason)
        except Exception as exc:  # pylint: disable=broad-except
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
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DeepSeek API helper
# ---------------------------------------------------------------------------
def _deepseek_chat(
    messages: List[dict],
    api_key: str,
    model: str = DEEPSEEK_MODEL,
    temperature: float = 0.2,
    max_tokens: int = 256,
    timeout: int = REQUEST_TIMEOUT,
) -> str:
    """Send a chat request to DeepSeek and return the assistant content."""
    payload = json.dumps({
        "model":       model,
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
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
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()

        except urllib.error.HTTPError as exc:
            logger.warning("DeepSeek HTTP %s on attempt %d: %s", exc.code, attempt, exc.reason)
        except urllib.error.URLError as exc:
            logger.warning("DeepSeek URL error on attempt %d: %s", attempt, exc.reason)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            logger.warning("DeepSeek response parse error on attempt %d: %s", attempt, exc)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("DeepSeek unexpected error on attempt %d: %s", attempt, exc)

        if attempt < RETRY_ATTEMPTS:
            time.sleep(1)

    raise RuntimeError("DeepSeek API call failed after all retry attempts.")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
def _parse_probability(text: str) -> Optional[float]:
    """Extract the first float in [0, 1] from the model's response."""
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


def _parse_response(text: str) -> Tuple[Optional[float], str]:
    """Parse DeepSeek response into (probability, reasoning).

    Handles the structured two-line format:
        PROBABILITY: 0.72
        REASONING: <text>

    Falls back to legacy bare-number extraction if the format is missing.
    Returns (prob_or_None, reasoning_str).
    """
    probability: Optional[float] = None
    reasoning: str = ""

    # --- Try structured format first ---
    prob_match = re.search(r"PROBABILITY\s*:\s*([0-9]*\.?[0-9]+)\s*%?", text, re.IGNORECASE)
    if prob_match:
        val = float(prob_match.group(1))
        if "%" in prob_match.group(0):
            val /= 100.0
        if 0.0 <= val <= 1.0:
            probability = round(val, 4)

    reasoning_match = re.search(r"REASONING\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if reasoning_match:
        reasoning = reasoning_match.group(1).strip()

    # --- Fallback: bare number extraction (legacy / non-compliant responses) ---
    if probability is None:
        probability = _parse_probability(text)

    # If no structured reasoning, use the full response as reasoning
    if not reasoning and probability is not None:
        reasoning = text.strip()

    return probability, reasoning


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------
def _build_messages(market: Market, news_context: str) -> List[dict]:
    from datetime import date
    today = date.today().isoformat()   # e.g. "2026-05-13"

    system_prompt = (
        f"Today's date is {today}. "
        "Use this date as your reference when assessing whether events have already "
        "occurred, whether deadlines have passed, or when reasoning about future outcomes. "
        "Do NOT assume any other date.\n\n"
        "You are a probability calibration expert for prediction markets. "
        "Your task is to estimate the true probability of a binary market outcome "
        "resolving YES.\n\n"
        "You MUST respond in the following exact format (two lines, nothing else):\n"
        "PROBABILITY: <decimal between 0 and 1, e.g. 0.72>\n"
        "REASONING: <one or two sentences explaining the key factors driving your estimate>"
    )

    user_content = f"""Market Question:
\"{market.question}\"

Current market price (implied probability): {market.price:.2%}
Category: {market.category}
Days to expiry: {market.days_to_expiry}

Latest News Context (from Brave Search):
{news_context}

Based on the news context above, estimate the TRUE probability this market resolves YES.
Respond ONLY in the required two-line format:
PROBABILITY: <number>
REASONING: <your analysis>"""

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
    timeout :
        HTTP request timeout in seconds (default: 15).
    model :
        DeepSeek model name to use (default: ``deepseek-chat``).
    max_results :
        Maximum number of Brave Search results to fetch (default: 5).
    temperature :
        Sampling temperature for DeepSeek (default: 0.2).
    max_tokens :
        Maximum tokens in the DeepSeek response (default: 256).
    """

    def __init__(
        self,
        deepseek_api_key:  Optional[str] = None,
        brave_api_key:     Optional[str] = None,
        fallback_on_error: bool = True,
        timeout:           Optional[int] = None,
        model:             Optional[str] = None,
        max_results:       Optional[int] = None,
        temperature:       Optional[float] = None,
        max_tokens:        Optional[int] = None,
    ):
        self.deepseek_key      = deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self.brave_key         = brave_api_key    or os.getenv("BRAVE_API_KEY", "")
        self.fallback_on_error = fallback_on_error

        self._timeout     = timeout     if timeout     is not None else REQUEST_TIMEOUT
        self._model       = model       if model       is not None else DEEPSEEK_MODEL
        self._max_results = max_results if max_results is not None else BRAVE_MAX_RESULTS
        self._temperature = temperature if temperature is not None else 0.2
        self._max_tokens  = max_tokens  if max_tokens  is not None else 256

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
        results = _brave_search(
            query, self.brave_key,
            max_results=self._max_results,
            timeout=self._timeout,
        )
        if not results:
            logger.warning("Brave Search returned no results for query: %r", query)
        return _format_news_context(results)

    def _estimate_prob(self, market: Market, news_context: str) -> Tuple[float, str]:
        """Call DeepSeek and parse the probability + reasoning.

        Returns (probability, reasoning_text). Raises on failure.
        """
        messages = _build_messages(market, news_context)
        response_text = _deepseek_chat(
            messages, self.deepseek_key,
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            timeout=self._timeout,
        )
        logger.debug("DeepSeek raw response for %r: %s", market.market_id, response_text)

        prob, reasoning = _parse_response(response_text)
        if prob is None:
            raise ValueError(
                f"Could not parse a probability from DeepSeek response: {response_text!r}"
            )
        return prob, reasoning

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def enrich(self, market: Market) -> Market:
        """Return a copy of `market` with AI-updated true_prob.

        If ``fallback_on_error=True`` and any error occurs, the original
        market (with its existing true_prob) is returned unchanged.
        """
        try:
            news_context        = self._get_news_context(market.question)
            new_prob, reasoning = self._estimate_prob(market, news_context)

            logger.info(
                "[%s] true_prob updated: %.4f → %.4f  (search: %s)",
                market.market_id,
                market.true_prob,
                new_prob,
                "yes" if self._brave_enabled else "no",
            )
            logger.info(
                "[%s] AI Reasoning: %s",
                market.market_id,
                reasoning,
            )

            import dataclasses
            return dataclasses.replace(market, true_prob=new_prob)

        except Exception as exc:  # pylint: disable=broad-except
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
