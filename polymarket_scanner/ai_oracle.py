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

    def _estimate_prob(self, market: Market, news_context: str) -> float:
        """Call DeepSeek and parse the probability; raises on failure."""
        messages = _build_messages(market, news_context)
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
            news_context = self._get_news_context(market.question)
            new_prob     = self._estimate_prob(market, news_context)

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

    def enrich_all(
        self,
        markets: List[Market],
        ai_top_n: int = 10,
        ai_min_liquidity: float = 100_000.0,
        ai_price_low: float = 0.10,
        ai_price_high: float = 0.90,
        bypass_filters: bool = False,
    ) -> List[Market]:
        """Enrich a list of markets with AI-estimated probabilities.

        Two-stage pipeline (skipped when bypass_filters=True)
        ------------------
        Stage 1 — Basic pre-filter
            Keep only markets that clear the minimum bars worth sending to
            a paid AI API:
              • liquidity  >= ai_min_liquidity  (default $100 000)
              • price in   (ai_price_low, ai_price_high)  (default 0.10–0.90)
                Extreme prices already have near-certain probabilities and
                produce negligible EV improvements from AI correction.

        Stage 2 — Hard Top-N cap
            Sort the pre-filtered candidates by liquidity DESC (most liquid
            markets have the tightest true-price signals and the most
            tradeable outcomes), then keep only the top ``ai_top_n``
            (default 10) for AI deep-analysis.

        Bypass markets
            • Markets that fail the basic pre-filter, AND
            • Markets that passed the pre-filter but ranked below Top-N
            …are returned unchanged with their original ``true_prob``.
            They are *not* discarded — the scanner still considers them
            with the raw market-implied probability.

        Parameters
        ----------
        markets :
            Full list of Market objects (all active markets fetched from
            Gamma API or mock data).
        ai_top_n :
            Hard cap on AI calls per run (default 10).
        ai_min_liquidity :
            Minimum USD liquidity for a market to be considered for AI
            enrichment (default $100 000).
        ai_price_low / ai_price_high :
            Price window outside which markets are considered too extreme
            for meaningful AI correction (default 0.10–0.90).
        bypass_filters : bool
            When True, skip Stage 1 (liquidity / price filter) and Stage 2
            (Top-N cap) entirely — every market in the list is sent to AI.
            Use this for position monitoring, where each market is already
            pre-selected and must receive a fresh AI probability regardless
            of its liquidity or current price level.
        """
        # ── bypass_filters 模式：跳过全部过滤，直接全量 AI 增强 ─────────
        if bypass_filters:
            logger.info(
                "AI 持仓模式（bypass_filters=True）：对 %d 个持仓市场跳过流动性/价格过滤，"
                "全量 AI 分析。",
                len(markets),
            )
            enriched: List[Market] = []
            for m in markets:
                enriched.append(self.enrich(m))
            return enriched

        # ── Stage 1: basic pre-filter ────────────────────────────────────
        ai_candidates: List[Market] = []
        bypass:        List[Market] = []

        for m in markets:
            if (
                m.liquidity >= ai_min_liquidity
                and ai_price_low < m.price < ai_price_high
            ):
                ai_candidates.append(m)
            else:
                bypass.append(m)

        n_basic = len(ai_candidates)

        # ── Stage 1.5: 黑马抽样 ──────────────────────────────────────────
        # 从被基础过滤剔除的市场中，找出流动性排名 Top-50 的候选，
        # 随机抽取 3 个"潜在黑马"送 AI——防止硬性过滤错失暴利机会。
        import random as _random
        DARK_HORSE_POOL   = 50   # 从流动性最高的 N 个被过滤市场中抽样
        DARK_HORSE_PICK   = 3    # 每次抽取数量
        dark_horse_ids: set = set()

        if bypass:
            # 按流动性降序排列被过滤市场，取前 DARK_HORSE_POOL 个作为候选池
            pool = sorted(bypass, key=lambda m: m.liquidity, reverse=True)[:DARK_HORSE_POOL]
            # 确保不重复，且不超过池大小
            picks = _random.sample(pool, min(DARK_HORSE_PICK, len(pool)))
            dark_horse_ids = {m.market_id for m in picks}
            # 将黑马加入待分析候选（插到队尾，不参与 Top-N 流动性排序）
            ai_candidates.extend(picks)
            logger.info(
                "🎲 黑马抽样：从 %d 个被过滤市场（流动性 Top-%d 池）随机抽取 %d 个送 AI：%s",
                len(bypass),
                min(DARK_HORSE_POOL, len(bypass)),
                len(picks),
                ", ".join(m.question[:30] + "…" for m in picks),
            )

        # ── Stage 2: sort by liquidity DESC + hard Top-N cut ─────────────
        # 黑马强制保留：正式候选取 ai_top_n 个（不受黑马影响），
        # 黑马在此基础上额外追加，总调用数 = ai_top_n + len(dark_horses)
        dark_horses = [m for m in ai_candidates if m.market_id in dark_horse_ids]
        regulars    = [m for m in ai_candidates if m.market_id not in dark_horse_ids]
        regulars.sort(key=lambda m: m.liquidity, reverse=True)

        top_n      = regulars[:ai_top_n] + dark_horses   # 正式 Top-N + 黑马（额外追加）
        tail       = regulars[ai_top_n:]                  # 剩余正式候选跳过 AI

        n_ai_calls = len(top_n)
        n_skipped  = len(bypass) - len(dark_horses) + len(tail)

        logger.info(
            "AI 前置过滤：%d 个符合基础条件（流动性 >= $%.0f，价格 %.0f%%–%.0f%%），"
            "已截取流动性 Top %d 进入 AI 深度分析，其余 %d 个直接使用原始概率。",
            n_basic,
            ai_min_liquidity,
            ai_price_low * 100,
            ai_price_high * 100,
            n_ai_calls,
            n_skipped,
        )

        if n_basic > ai_top_n:
            logger.info(
                "  → Top %d 流动性范围：$%.0f – $%.0f",
                n_ai_calls,
                top_n[-1].liquidity if top_n else 0,
                top_n[0].liquidity  if top_n else 0,
            )

        # ── AI enrichment (only Top-N) ────────────────────────────────────
        enriched_top: List[Market] = []
        for m in top_n:
            enriched_top.append(self.enrich(m))

        # ── Reassemble: preserve original market order ────────────────────
        enriched_index = {m.market_id: m for m in enriched_top}
        result: List[Market] = []
        for m in markets:
            result.append(enriched_index.get(m.market_id, m))

        return result
