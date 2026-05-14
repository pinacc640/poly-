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

import html as _html
import json
import logging
import os
import re
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
# Exceptions
# ---------------------------------------------------------------------------
class BraveQuotaExceeded(Exception):
    """Raised when Brave Search API returns HTTP 402 (quota exhausted)."""
    pass


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
                # urllib may return gzip-compressed bytes
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
            if exc.code == 402:
                raise BraveQuotaExceeded(f"Brave API quota exceeded (HTTP 402)")
            logger.warning("Brave Search HTTP %s on attempt %d: %s", exc.code, attempt, exc.reason)
        except urllib.error.URLError as exc:
            logger.warning("Brave Search URL error on attempt %d: %s", attempt, exc.reason)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Brave Search unexpected error on attempt %d: %s", attempt, exc)

        if attempt < RETRY_ATTEMPTS:
            time.sleep(1)

    return []


# ---------------------------------------------------------------------------
# DuckDuckGo HTML fallback search
# ---------------------------------------------------------------------------
def _duckduckgo_search(
    query: str,
    max_results: int = BRAVE_MAX_RESULTS,
    timeout: int = REQUEST_TIMEOUT,
) -> List[dict]:
    """Fallback search via DuckDuckGo HTML interface.

    Returns list of dicts with 'title' and 'description' keys (same format
    as _brave_search).  Returns empty list on any error.
    """
    try:
        form_data = urllib.parse.urlencode({"q": query}).encode("utf-8")
        req = urllib.request.Request(
            "https://html.duckduckgo.com/html/",
            data=form_data,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html_body = resp.read().decode("utf-8", errors="replace")

        # Extract titles from <a class="result__a" ...>...</a>
        titles = re.findall(r'class="result__a"[^>]*>(.+?)</a>', html_body, re.DOTALL)
        # Extract snippets from <a class="result__snippet" ...>...</a>
        snippets = re.findall(r'class="result__snippet"[^>]*>(.+?)</a>', html_body, re.DOTALL)

        if not titles and html_body:
            logger.warning("DuckDuckGo HTML parsed 0 results — markup may have changed")

        results = []
        for i in range(min(len(titles), max_results)):
            title = re.sub(r'<[^>]+>', '', titles[i])
            title = _html.unescape(title).strip()
            desc = ""
            if i < len(snippets):
                desc = re.sub(r'<[^>]+>', '', snippets[i])
                desc = _html.unescape(desc).strip()
            results.append({"title": title, "description": desc})

        logger.debug("DuckDuckGo search returned %d results for query: %r", len(results), query)
        return results

    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("DuckDuckGo search failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Google HTML fallback search
# ---------------------------------------------------------------------------
def _google_search(
    query: str,
    max_results: int = BRAVE_MAX_RESULTS,
    timeout: int = REQUEST_TIMEOUT,
) -> List[dict]:
    """Fallback search via Google HTML scraping.

    Scrapes Google search results page for titles (<h3> tags) and
    descriptions from nearby text. Returns list of dicts with 'title'
    and 'description' keys (same format as _brave_search).
    Returns empty list on any error.
    """
    try:
        params = urllib.parse.urlencode({"q": query, "num": max_results, "hl": "en"})
        url = f"https://www.google.com/search?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        import ssl as _ssl
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE

        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            html_body = resp.read().decode("utf-8", errors="replace")

        # Extract titles from <h3> tags
        h3_matches = re.findall(r'<h3[^>]*>(.*?)</h3>', html_body, re.DOTALL)

        # Extract descriptions: text in <span> elements near results
        # Google wraps descriptions in spans within result divs
        desc_matches = re.findall(
            r'<div[^>]*class="[^"]*VwiC3b[^"]*"[^>]*>(.*?)</div>',
            html_body, re.DOTALL
        )
        # Fallback: try data-sncf pattern used in some Google layouts
        if not desc_matches:
            desc_matches = re.findall(
                r'<span[^>]*class="[^"]*st[^"]*"[^>]*>(.*?)</span>',
                html_body, re.DOTALL
            )

        if not h3_matches and html_body:
            logger.warning("Google HTML parsed 0 results - markup may have changed or request was blocked")

        results = []
        for i in range(min(len(h3_matches), max_results)):
            title = re.sub(r'<[^>]+>', '', h3_matches[i])
            title = _html.unescape(title).strip()
            desc = ""
            if i < len(desc_matches):
                desc = re.sub(r'<[^>]+>', '', desc_matches[i])
                desc = _html.unescape(desc).strip()
            results.append({"title": title, "description": desc})

        logger.debug("Google search returned %d results for query: %r", len(results), query)
        return results

    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("Google search failed: %s", exc)
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


def _parse_probability(text: str) -> Optional[float]:
    """Extract the first float in [0, 1] from the model's response."""
    import re
    # Match patterns like 0.73, .85, 73%, 0.73.
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
def _build_messages(market: Market, news_context: str, source_label: str = "Brave Search") -> List[dict]:
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

Latest News Context (from {source_label}):
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
    timeout :
        HTTP request timeout in seconds (default: REQUEST_TIMEOUT = 15).
        Overrides the module-level constant for both Brave and DeepSeek calls.
    model :
        DeepSeek model name to use (default: ``deepseek-chat``).
    max_results :
        Maximum number of Brave Search results to fetch (default: 5).
    temperature :
        Sampling temperature for DeepSeek (default: 0.2).
    max_tokens :
        Maximum tokens in the DeepSeek response (default: 256).
    """

    _brave_exhausted: bool = False

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

        # Tuneable overrides (fall back to module-level defaults when None)
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
    def _get_news_context(self, query: str) -> tuple:
        """Fetch news via Brave; fall back to Google/DuckDuckGo on quota exceeded or empty results.

        Returns a tuple of (source_label, context_text) where source_label is
        "Brave Search", "Google" or "DuckDuckGo".
        """
        if not self._brave_enabled or self._brave_exhausted:
            # No Brave key or quota exhausted - try Google then DuckDuckGo
            results = _google_search(query, max_results=self._max_results, timeout=self._timeout)
            if results:
                time.sleep(1.5)
                return ("Google", _format_news_context(results))
            # Google failed, try DuckDuckGo as secondary
            results = _duckduckgo_search(query, max_results=self._max_results, timeout=self._timeout)
            time.sleep(1.5)
            if results:
                return ("DuckDuckGo", _format_news_context(results))
            return ("Google", "(Live search disabled - no search results available.)")

        try:
            results = _brave_search(
                query, self.brave_key,
                max_results=self._max_results,
                timeout=self._timeout,
            )
        except BraveQuotaExceeded:
            AIOracle._brave_exhausted = True
            logger.warning("Brave API 配额耗尽，本次会话将使用备用搜索引擎")
            results = _google_search(query, max_results=self._max_results, timeout=self._timeout)
            if results:
                time.sleep(1.5)
                return ("Google", _format_news_context(results))
            results = _duckduckgo_search(query, max_results=self._max_results, timeout=self._timeout)
            time.sleep(1.5)
            return ("DuckDuckGo", _format_news_context(results))

        if not results:
            logger.warning("Brave Search returned no results for query: %r, trying Google", query)
            results = _google_search(query, max_results=self._max_results, timeout=self._timeout)
            if results:
                time.sleep(1.5)
                return ("Google", _format_news_context(results))
            results = _duckduckgo_search(query, max_results=self._max_results, timeout=self._timeout)
            time.sleep(1.5)
            return ("DuckDuckGo", _format_news_context(results))

        return ("Brave Search", _format_news_context(results))

    def _estimate_prob(self, market: Market, news_context: str, source_label: str = "Brave Search") -> float:
        """Call DeepSeek and parse the probability; raises on failure."""
        messages = _build_messages(market, news_context, source_label=source_label)
        response_text = _deepseek_chat(
            messages, self.deepseek_key,
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            timeout=self._timeout,
        )
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
            source_label, news_context = self._get_news_context(market.question)
            new_prob = self._estimate_prob(market, news_context, source_label=source_label)

            logger.info(
                "[%s] true_prob updated: %.4f → %.4f  (search: %s)",
                market.market_id,
                market.true_prob,
                new_prob,
                "yes" if self._brave_enabled else "no",
            )

            # Return a shallow copy with updated true_prob
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
