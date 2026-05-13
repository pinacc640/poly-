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


def _build_prompt(question: str, price: float, ai_prob: float, side: str = "YES") -> str:
    """Build a focused prompt for DeepSeek fundamentals check.

    Parameters
    ----------
    side : "YES" or "NO"
        The trade direction proposed by the scanner.
        "NO" means we are shorting the market (buying the NO token),
        betting that the market price is inflated by irrational hype.
    """
    edge_pct = (ai_prob - price) * 100
    if side == "NO":
        # Buying NO: our AI thinks the true probability is LOWER than market price.
        # The "edge" is negative (price > ai_prob); express it as the overpricing.
        trade_desc = (
            f"Proposed trade: BUY NO (short the market)\n"
            f"The scanner believes the market is OVERPRICED by {abs(edge_pct):.1f} "
            f"percentage points ({price:.1%} market vs {ai_prob:.1%} AI estimate).\n"
            f"This is a SHORT trade — we profit if the market price falls."
        )
    else:
        trade_desc = (
            f"Proposed trade: BUY YES (long the market)\n"
            f"The scanner believes the market is UNDERPRICED by {abs(edge_pct):.1f} "
            f"percentage points ({price:.1%} market vs {ai_prob:.1%} AI estimate).\n"
            f"This is a LONG trade — we profit if the market price rises."
        )

    return (
        f"Market: \"{question}\"\n"
        f"Current price (implied probability): {price:.1%}\n"
        f"AI-estimated true probability: {ai_prob:.1%}\n"
        f"{trade_desc}\n\n"
        f"IMPORTANT RULES for your verdict:\n\n"
        f"Return FAIL ONLY if one of these two hard conditions is true:\n"
        f"  1. INSIDER TRAP: There is concrete, specific, undiscounted information "
        f"(e.g., a confirmed player injury, a privately announced withdrawal) that "
        f"fully explains the price and eliminates the edge.\n"
        f"  2. RESOLUTION AMBIGUITY: The market resolution criteria are genuinely "
        f"disputed, unclear, or subject to contradictory official interpretations.\n\n"
        f"Return PASS in ALL other cases, including:\n"
        f"  - The price is high due to irrational public enthusiasm, hype, or fake news "
        f"(for NO trades, this IS the opportunity — crowd irrationality is the edge).\n"
        f"  - The event is upcoming and uncertain.\n"
        f"  - General uncertainty or mixed opinions exist.\n"
        f"  - The market has not resolved yet.\n\n"
        f"Do NOT fail a NO trade simply because the market price is high or because "
        f"the outcome seems possible. Overpriced markets driven by hype are exactly "
        f"what NO trades are designed to exploit.\n\n"
        f"Respond in this exact format:\n"
        f"VERDICT: PASS or FAIL\n"
        f"REASON: <one sentence — cite the specific hard condition if FAIL, "
        f"or confirm no hard conditions exist if PASS>"
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
        # Fallback: only treat as FAIL on the two hard conditions we defined.
        # Keywords that unambiguously signal a hard condition — we require they
        # appear WITHOUT a preceding negation (no, not, without, lacks).
        lower = text.lower()

        def _hard_condition_present(keyword: str) -> bool:
            """True if keyword present and not negated by nearby 'no/not/without'."""
            idx = lower.find(keyword)
            if idx == -1:
                return False
            # Look at the 40 chars before the keyword for negating words
            prefix = lower[max(0, idx - 40): idx]
            negations = ("no ", "not ", "without ", "lacks ", "no concrete ", "no specific ")
            return not any(neg in prefix for neg in negations)

        hard_conditions = [
            "confirmed insider",
            "insider information",
            "confirmed injury",
            "resolution ambigui",
            "genuinely disputed",
            "already resolved",
            "already settled",
            "verdict: fail",
        ]
        passed = not any(_hard_condition_present(kw) for kw in hard_conditions)
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

    def check(self, market, ai_prob: float, side: str = "YES") -> FundamentalsVerdict:
        """Run a fundamentals sanity check on a single market.

        Parameters
        ----------
        market :
            A Market object (needs .question, .price, .market_id).
        ai_prob :
            The AI-estimated true probability (from oracle or true_prob field).
        side : "YES" or "NO"
            Trade direction. "NO" means we are shorting (buying the NO token);
            the prompt instructs DeepSeek that hype-driven overpricing is the
            opportunity, not a trap.

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

        prompt = _build_prompt(market.question, market.price, ai_prob, side=side)

        from datetime import date
        today = date.today().isoformat()   # e.g. "2026-05-13"
        system_content = (
            f"Today's date is {today}. "
            "Use this date as your ground truth when evaluating whether events have "
            "already happened, are upcoming, or whether any deadlines have passed. "
            "Do NOT assume any other date.\n\n"
            "You are a concise prediction-market risk analyst. "
            "Your job is to PASS trade signals unless you can identify a specific, "
            "concrete hard condition. When in doubt, PASS."
        )

        payload = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_content},
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
        The trade direction (YES/NO) is extracted from opp.rationale so the
        prompt can apply the correct logic for long vs short trades.
        """
        if not self.is_available():
            log.info("FundamentalsChecker: DEEPSEEK_API_KEY not set — skipping all checks")
            return opportunities

        passed = []
        for item in opportunities:
            opp = item[0] if isinstance(item, tuple) else item
            market = opp.market
            ai_prob = market.true_prob

            # Extract trade side from rationale (set by strategy layer)
            side = "YES"
            for note in getattr(opp, "rationale", []):
                if "side=NO" in note or "direction=SELL" in note or "(side=NO)" in note:
                    side = "NO"
                    break
                if "side=YES" in note or "direction=BUY" in note or "(side=YES)" in note:
                    side = "YES"
                    break

            verdict = self.check(market, ai_prob, side=side)

            if verdict.pass_check:
                passed.append(item)
                log.debug("[%s] Fundamentals PASS (%s): %s",
                          market.market_id, side, verdict.reasoning)
            else:
                log.warning(
                    "[%s] Fundamentals FAIL (%s) — alert blocked: %s",
                    market.market_id, side, verdict.reasoning,
                )

        log.info(
            "Fundamentals check: %d/%d passed",
            len(passed), len(opportunities),
        )
        return passed
