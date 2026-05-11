"""Kalshi public REST API client — no authentication required.

Kalshi base URL : https://api.elections.kalshi.com/trade-api/v2
All endpoints are public for read-only market data.

Key fields returned per market
-------------------------------
ticker            : unique market ID (str)
title             : human-readable question (str)
yes_ask_dollars   : best ask for YES share in USD, e.g. "0.6200" (str → float)
no_ask_dollars    : best ask for NO  share in USD, e.g. "0.4100" (str → float)
close_time        : ISO-8601 expiry datetime (str)
volume_24h_fp     : 24h volume in fractional contracts (str → float)
status            : "active" | "closed" | "settled"

Note: Kalshi prices are in USD (0.00–1.00), same scale as Polymarket.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
_PAGE_SIZE      = 100
_INTER_PAGE_SLEEP = 0.25
_MAX_PAGES      = 20


def _build_session(retries: int = 3) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "poly-scanner/1.0",
    })
    return session


class KalshiClient:
    """Thin wrapper around the Kalshi public REST API.

    Parameters
    ----------
    base_url : override for testing
    timeout  : per-request timeout in seconds
    retries  : automatic retry count on transient errors
    """

    def __init__(
        self,
        base_url: str = KALSHI_BASE_URL,
        timeout:  float = 15.0,
        retries:  int   = 3,
    ):
        self.base_url  = base_url.rstrip("/")
        self.timeout   = timeout
        self._session  = _build_session(retries=retries)

    # ------------------------------------------------------------------
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(f"Network error reaching Kalshi API: {exc}") from exc
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f"Kalshi API timed out after {self.timeout}s") from exc

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 60))
            log.warning("Kalshi rate-limited. Waiting %ds …", wait)
            time.sleep(wait)
            raise RuntimeError("Kalshi rate limited (429).")

        if not resp.ok:
            raise RuntimeError(
                f"Kalshi API returned HTTP {resp.status_code} for {url}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Kalshi non-JSON response: {resp.text[:200]}") from exc

    # ------------------------------------------------------------------
    def fetch_active_markets(
        self,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return up to `limit` active (open) Kalshi markets.

        Skips multi-leg parlay markets (ticker prefix KXMVE*) because
        their composite titles do not match individual Polymarket questions.
        """
        collected: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        for page_num in range(_MAX_PAGES):
            if len(collected) >= limit:
                break

            params: Dict[str, Any] = {
                "status": "open",
                "limit":  min(_PAGE_SIZE, limit - len(collected)),
            }
            if cursor:
                params["cursor"] = cursor

            try:
                data = self._get("/markets", params=params)
            except RuntimeError as exc:
                if collected:
                    log.warning(
                        "Kalshi page %d failed (%s). Returning %d collected.",
                        page_num + 1, exc, len(collected),
                    )
                    break
                raise

            page: List[Dict] = data.get("markets", [])
            if not page:
                break

            # Filter out composite parlay markets — their titles are
            # comma-joined and won't match a single Polymarket question.
            singles = [
                m for m in page
                if not m.get("ticker", "").startswith("KXMVE")
            ]
            collected.extend(singles)

            cursor = data.get("cursor")
            if not cursor or len(page) < _PAGE_SIZE:
                break

            time.sleep(_INTER_PAGE_SLEEP)

        log.info("Fetched %d active single-outcome markets from Kalshi.", len(collected))
        return collected[:limit]

    # ------------------------------------------------------------------
    def health_check(self) -> bool:
        """Return True if the API is reachable."""
        try:
            self._get("/markets", params={"status": "open", "limit": 1})
            return True
        except RuntimeError:
            return False


# ---------------------------------------------------------------------------
# Kalshi market → normalised dict used by arbitrage strategy
# ---------------------------------------------------------------------------
def normalise_kalshi_market(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a raw Kalshi market dict to a minimal normalised form.

    Returns None if essential fields are missing or prices are zero/invalid.
    """
    ticker    = raw.get("ticker", "")
    title     = (raw.get("title") or "").strip()
    if not ticker or not title:
        return None

    try:
        yes_ask = float(raw.get("yes_ask_dollars") or 0)
        no_ask  = float(raw.get("no_ask_dollars")  or 0)
    except (TypeError, ValueError):
        return None

    # Skip markets with no meaningful price (zero means no quote)
    if yes_ask <= 0 and no_ask <= 0:
        return None

    # If one side is missing, infer from the other
    if yes_ask <= 0:
        yes_ask = round(1.0 - no_ask, 4)
    if no_ask <= 0:
        no_ask  = round(1.0 - yes_ask, 4)

    return {
        "ticker":     ticker,
        "title":      title,
        "yes_ask":    yes_ask,
        "no_ask":     no_ask,
        "close_time": raw.get("close_time", ""),
        "volume_24h": float(raw.get("volume_24h_fp") or 0),
    }
