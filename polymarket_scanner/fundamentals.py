"""Fundamentals checker — DeepSeek API quick sanity check on high-EV signals.

Before pushing a Telegram alert, this module asks DeepSeek (via its
official API) a focused question: "Given the latest news, is this
market's current price reasonable or is there a known reason it should
be trading at this level?"

This catches obvious traps:
  - Markets priced low because they already resolved (but haven't settled)
  - Markets with known insider info that makes the "edge" illusory
  - Markets where the question is ambiguous and resolution is disputed

Environment Variables
---------------------
DEEPSEEK_API_KEY  (required) : DeepSeek API key for chat completions.

Usage
-----
    from polymarket_scanner.fundamentals import FundamentalsChecker

    checker = FundamentalsChecker()
    if checker.is_available():
        verdict = checker.check(market, ai_prob=0.85)
        # verdict.pass_check  → True/False
        # verdict.reasoning   → "No red flags found. Price seems..."
"""

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
REQUEST_TIMEOUT = 20  # seconds


@dataclass
class FundamentalsVerdict:
    """Result of a fundamentals sanity check."""
    pass_check: bool        # True = no red flags, safe to alert
    reasoning: str          # One-paragraph explanation from DeepSeek
    market_id: str          # Which market was checked


def _build_prompt(question: str, price: float, ai_prob: float) -> str:
    """Build a focused prompt for DeepSeek fundamentals check."""
    return (
        f"You are a risk analyst for prediction markets. "
        f"A scanner flagged this market as a potential trade opportunity.\n\n"
        f"Market: \"{question}\"\n"
        f"Current price (implied probability): {price:.1%}\n"
        f"AI-estimated true probability: {ai_prob:.1%}\n"
        f"Perceived edge: {(ai_prob - price)*100:+.1f} percentage points\n\n"
        f"Please do a quick sanity check:\n"
        f"1. Is there any widely-known news that explains why this price is correct "
        f"(i.e., the 'edge' is actually a trap)?\n"
        f"2. Has this event already effectively resolved but not yet settled on-chain?\n"
        f"3. Are there any red flags (ambiguous resolution criteria, disputed outcome, etc.)?\n\n"
        f"Respond in this exact format:\n"
        f"VERDICT: PASS or FAIL\n"
        f"REASON: <one or two sentences explaining your conclusion>"
    )


def _parse_verdict(response_text: str, market_id: str) -> FundamentalsVerdict:
    """Parse DeepSeek response into a FundamentalsVerdict."""
    import re

    text = response_text.strip()

    # Try structured format
    verdict_match = re.search(r"VERDICT\s*:\s*(PASS|FAIL)", text, re.IGNORECASE)
    reason_match = re.search(r"REASON\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)

    if verdict_match:
        passed = verdict_match.group(1).upper() == "PASS"
        reasoning = reason_match.group(1).strip() if reason_match else text
    else:
        # Fallback: if response contains "fail" or "red flag", treat as fail
        lower = text.lower()
        passed = not any(w in lower for w in ["fail", "red flag", "already resolved", "trap"])
        reasoning = text

    return FundamentalsVerdict(
        pass_check=passed,
        reasoning=reasoning[:500],  # cap length for Telegram
        market_id=market_id,
    )


class FundamentalsChecker:
    """Quick DeepSeek-based sanity check on high-EV opportunities.

    Parameters
    ----------
    api_key :
        DeepSeek API key. Falls back to env var DEEPSEEK_API_KEY.
    timeout :
        Request timeout in seconds.
    model :
        DeepSeek model name.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = REQUEST_TIMEOUT,
        model: str = DEEPSEEK_MODEL,
    ):
        self._key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self._timeout = timeout
        self._model = model

    def is_available(self) -> bool:
        """Return True if DeepSeek API key is configured."""
        return bool(self._key)

    def check(self, market, ai_prob: float) -> FundamentalsVerdict:
        """Run a fundamentals sanity check on a single market.

        Parameters
        ----------
        market :
            A Market object (needs .question, .price, .market_id).
        ai_prob :
            The AI-estimated true probability (from oracle or true_prob field).

        Returns
        -------
        FundamentalsVerdict with pass_check and reasoning.
        On API failure, returns a PASS verdict with a warning note
        (fail-open: we don't block alerts just because DeepSeek is down).
        """
        if not self._key:
            return FundamentalsVerdict(
                pass_check=True,
                reasoning="(Fundamentals check skipped — DEEPSEEK_API_KEY not set)",
                market_id=market.market_id,
            )

        prompt = _build_prompt(market.question, market.price, ai_prob)

        payload = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": "You are a concise prediction-market risk analyst."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 200,
        }).encode("utf-8")

        req = urllib.request.Request(
            DEEPSEEK_API_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            response_text = data["choices"][0]["message"]["content"].strip()
            log.debug("DeepSeek fundamentals response for %s: %s", market.market_id, response_text)
            return _parse_verdict(response_text, market.market_id)

        except urllib.error.HTTPError as exc:
            log.warning("DeepSeek HTTP %s during fundamentals check: %s", exc.code, exc.reason)
        except urllib.error.URLError as exc:
            log.warning("DeepSeek URL error during fundamentals check: %s", exc.reason)
        except Exception as exc:  # pylint: disable=broad-except
            log.warning("DeepSeek unexpected error during fundamentals check: %s", exc)

        # Fail-open: don't block alerts just because AI is down
        return FundamentalsVerdict(
            pass_check=True,
            reasoning="(Fundamentals check failed — API error, proceeding with alert)",
            market_id=market.market_id,
        )

    def check_opportunities(self, opportunities: list) -> list:
        """Check a list of (opp, decision) tuples, return only those that pass.

        Opportunities that FAIL the fundamentals check are logged and dropped.
        """
        if not self.is_available():
            log.info("FundamentalsChecker: DEEPSEEK_API_KEY not set — skipping all checks")
            return opportunities

        passed = []
        for item in opportunities:
            opp = item[0] if isinstance(item, tuple) else item
            market = opp.market
            ai_prob = market.true_prob

            verdict = self.check(market, ai_prob)

            if verdict.pass_check:
                passed.append(item)
                log.debug("[%s] Fundamentals PASS: %s", market.market_id, verdict.reasoning)
            else:
                log.warning(
                    "[%s] Fundamentals FAIL — alert blocked: %s",
                    market.market_id, verdict.reasoning,
                )

        log.info(
            "Fundamentals check: %d/%d passed",
            len(passed), len(opportunities),
        )
        return passed
