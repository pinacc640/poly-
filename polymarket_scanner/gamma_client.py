"""Polymarket Gamma API client.

Public REST API — no key, no auth needed.
Base URL: https://gamma-api.polymarket.com

Rate limit: ~100 req/s (firm-wide).  We stay well below that by
sleeping between paginated pages and using exponential backoff on
transient errors.

Typical usage
-------------
    from polymarket_scanner.gamma_client import GammaClient

    client = GammaClient()
    raw_markets = client.fetch_active_markets(limit=200)
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

# How many markets to pull per HTTP request (Gamma max observed = 500)
_PAGE_SIZE = 200

# Seconds to wait between paginated pages — keeps us comfortably under
# the 100 req/s limit even when fetching many pages.
_INTER_PAGE_SLEEP = 0.25

# Exponential backoff: wait this many seconds before first retry,
# then doubles each attempt.
_BACKOFF_BASE = 1.0

# Total pages cap — prevents infinite loops on misconfigured calls.
_MAX_PAGES = 50


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
def _build_session(retries: int = 3, backoff: float = _BACKOFF_BASE) -> requests.Session:
    """Return a requests.Session with automatic retry on network errors."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        # Retry on these HTTP status codes (server-side transients)
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"Accept": "application/json", "User-Agent": "poly-scanner/1.0"})
    return session


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class GammaClient:
    """Thin wrapper around the Gamma REST API.

    Parameters
    ----------
    base_url :
        Override for testing against a mock server.
    timeout :
        Per-request timeout in seconds.
    retries :
        Number of automatic retries on transient HTTP errors.
    """

    def __init__(
        self,
        base_url: str = GAMMA_BASE_URL,
        timeout: float = 15.0,
        retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = _build_session(retries=retries)

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET {base_url}/{path} with error handling.

        Returns parsed JSON on success.
        Raises RuntimeError on unrecoverable HTTP errors.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(f"Network error reaching Gamma API: {exc}") from exc
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(f"Gamma API timed out after {self.timeout}s") from exc

        if resp.status_code == 429:
            # Explicit rate-limit response — wait and surface a clear message.
            retry_after = int(resp.headers.get("Retry-After", 60))
            log.warning("Rate limited by Gamma API. Waiting %ds …", retry_after)
            time.sleep(retry_after)
            raise RuntimeError("Rate limited (429). Retry after back-off.")

        if not resp.ok:
            raise RuntimeError(
                f"Gamma API returned HTTP {resp.status_code} for {url}: {resp.text[:200]}"
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Gamma API returned non-JSON response: {resp.text[:200]}") from exc

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------
    def fetch_active_markets(
        self,
        limit: int = 200,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> List[Dict[str, Any]]:
        """Fetch active, non-closed markets ordered by 24h volume.

        Automatically paginates until `limit` records are collected or
        there are no more results.

        Parameters
        ----------
        limit :
            Maximum number of market records to return.
        order :
            Field to sort by.  ``volume24hr`` gives the most liquid
            markets first — ideal for a small-capital scanner.
        ascending :
            Sort direction.

        Returns
        -------
        List of raw market dicts (Gamma API shape).
        """
        collected: List[Dict[str, Any]] = []
        offset = 0
        page_size = min(_PAGE_SIZE, limit)

        for page_num in range(_MAX_PAGES):
            remaining = limit - len(collected)
            if remaining <= 0:
                break

            batch_size = min(page_size, remaining)
            params: Dict[str, Any] = {
                "active": "true",
                "closed": "false",
                "limit": batch_size,
                "offset": offset,
                "order": order,
                "ascending": str(ascending).lower(),
            }

            log.debug("Fetching page %d (offset=%d, size=%d) …", page_num + 1, offset, batch_size)
            try:
                page = self._get("/markets", params=params)
            except RuntimeError as exc:
                if collected:
                    log.warning("Page %d failed (%s). Returning %d already collected.",
                                page_num + 1, exc, len(collected))
                    break
                raise   # No data at all → propagate

            if not page:
                log.debug("Empty page — no more results.")
                break

            collected.extend(page)
            offset += len(page)

            if len(page) < batch_size:
                log.debug("Short page (%d < %d) — reached end of results.", len(page), batch_size)
                break

            if page_num < _MAX_PAGES - 1:
                time.sleep(_INTER_PAGE_SLEEP)

        log.info("Fetched %d active markets from Gamma API.", len(collected))
        return collected

    def health_check(self) -> bool:
        """Return True if the API is reachable."""
        try:
            self._get("/markets", params={"active": "true", "closed": "false", "limit": 1})
            return True
        except RuntimeError:
            return False
