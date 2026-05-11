"""AI Oracle — DeepSeek-V3 via SiliconFlow for true-probability estimation.

Usage
-----
    oracle = AiOracle()                   # reads SILICONFLOW_API_KEY from env
    prob   = oracle.estimate_prob(
        question="Will the Fed cut rates in June 2026?",
        current_price=0.35,
    )
    # prob is a float in [0.01, 0.99]

Design decisions
----------------
- The prompt instructs the model to act as a **financial analyst** and
  return a *single decimal number* only.  Any prose causes the response
  parser to fall back to `current_price`.
- Responses are cached in-process (LRU, 256 slots) so repeated calls for
  the same question within one scan run never hit the API twice.
- All network errors are caught and logged; the fallback value is always
  `current_price` so downstream strategy code never sees None / exception.
- Timeout is configurable (default 20 s) to keep scan runs snappy.
"""

import logging
import os
import re
from functools import lru_cache
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_BASE_URL    = "https://api.siliconflow.cn/v1"
_MODEL       = "deepseek-ai/DeepSeek-V3"
_ENV_KEY     = "SILICONFLOW_API_KEY"
_DEFAULT_TIMEOUT = 20          # seconds per API call
_MAX_TOKENS  = 16              # we only need one number back

_SYSTEM_PROMPT = (
    "You are a professional financial analyst specialising in prediction markets. "
    "When given a market question and its current traded price, you estimate the "
    "TRUE probability of the YES outcome based on your knowledge of the underlying "
    "event. "
    "Rules:\n"
    "1. Reply with ONLY a single decimal number between 0.01 and 0.99.\n"
    "2. Do NOT include any explanation, units, or surrounding text.\n"
    "3. Examples of valid responses: 0.72  |  0.08  |  0.55\n"
)

_USER_TEMPLATE = (
    "Market question: {question}\n"
    "Current market price (implied probability): {price:.3f}\n"
    "What is the true probability? Reply with a single decimal number only."
)


# ---------------------------------------------------------------------------
# Oracle class
# ---------------------------------------------------------------------------
class AiOracle:
    """Wraps the SiliconFlow / DeepSeek-V3 API for probability estimation.

    Parameters
    ----------
    api_key :
        Overrides ``SILICONFLOW_API_KEY`` env var.  Pass explicitly in tests.
    timeout :
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._api_key = api_key or os.getenv(_ENV_KEY, "")
        self._timeout = timeout
        self._client  = None          # lazy-init so import never fails

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        """Return True if an API key is configured."""
        return bool(self._api_key)

    def estimate_prob(
        self,
        question: str,
        current_price: float,
    ) -> float:
        """Return AI-estimated true probability for *question*.

        Falls back to *current_price* on any error so callers are
        always guaranteed a valid float in [0.01, 0.99].
        """
        if not self.is_available():
            log.warning("[AiOracle] No API key — returning market price as true_prob.")
            return _clamp(current_price)

        # Use cached helper (keyed on question text + rounded price)
        return self._cached_estimate(question, round(current_price, 3))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _get_client(self):
        """Lazy-init the OpenAI client."""
        if self._client is None:
            try:
                from openai import OpenAI  # type: ignore
            except ImportError:
                raise RuntimeError(
                    "openai package is not installed. "
                    "Run: pip install openai"
                )
            self._client = OpenAI(
                api_key=self._api_key,
                base_url=_BASE_URL,
                timeout=self._timeout,
            )
        return self._client

    def _cached_estimate(self, question: str, price: float) -> float:
        """LRU-cached probability estimate keyed on (question, price)."""
        return _estimate_cached(self._get_client, question, price)

    def batch_estimate(
        self,
        markets,            # List[Market]
        max_workers: int = 5,
    ) -> None:
        """Update ``true_prob`` in-place for every market in the list.

        Uses a simple sequential loop (no threading) to avoid rate-limit
        bursts.  For large batches consider adding a per-call sleep.
        """
        if not self.is_available():
            log.warning("[AiOracle] No API key — skipping batch estimation.")
            return

        total = len(markets)
        for i, market in enumerate(markets, 1):
            try:
                prob = self.estimate_prob(market.question, market.price)
                market.true_prob = prob
                log.debug(
                    "[AiOracle] %d/%d  %s  → %.3f  (was %.3f)",
                    i, total, market.market_id, prob, market.price,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "[AiOracle] %d/%d  %s  failed: %s — keeping price as true_prob",
                    i, total, market.market_id, exc,
                )


# ---------------------------------------------------------------------------
# Module-level LRU cache (survives multiple AiOracle instances in one run)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=256)
def _estimate_cached(get_client_fn, question: str, price: float) -> float:
    """Cached inner call.  get_client_fn is hashable (bound method)."""
    client = get_client_fn()
    user_msg = _USER_TEMPLATE.format(question=question, price=price)
    try:
        response = client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            max_tokens=_MAX_TOKENS,
            temperature=0.0,        # deterministic output
        )
        raw = response.choices[0].message.content.strip()
        return _parse_prob(raw, fallback=price)
    except Exception as exc:  # noqa: BLE001
        log.warning("[AiOracle] API call failed: %s — using market price %.3f", exc, price)
        return _clamp(price)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_prob(text: str, fallback: float) -> float:
    """Extract the first valid float from *text* and clamp to [0.01, 0.99]."""
    # Match a decimal number, e.g. "0.72" or ".72" or "72" (treated as 0.72)
    match = re.search(r"\b(0?\.\d+|\d+\.\d*|\d+)\b", text)
    if not match:
        log.warning("[AiOracle] Could not parse response %r — using fallback %.3f", text, fallback)
        return _clamp(fallback)
    value = float(match.group(1))
    # If model returned e.g. "72" treat it as 0.72
    if value > 1.0:
        value = value / 100.0
    return _clamp(value)


def _clamp(v: float) -> float:
    return max(0.01, min(0.99, float(v)))
