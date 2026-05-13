"""Push deduplication — prevent repeated Telegram alerts for the same market.

Stores pushed market IDs with timestamps in a local JSON file.
Markets pushed within the last 24 hours are considered "already notified"
and will be filtered out before sending Telegram alerts.

Usage
-----
    from polymarket_scanner.dedup import PushDedup

    dedup = PushDedup()                     # uses default ./pushed_markets.json
    new_opps = dedup.filter_new(approved)   # returns only never-pushed opportunities
    dedup.mark_pushed(new_opps)             # record them so next run skips them

File format (pushed_markets.json)
---------------------------------
{
    "market_id_1": 1715520000.0,   // unix timestamp of last push
    "market_id_2": 1715510000.0,
    ...
}

Stale entries (older than 24h) are automatically purged on each load.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)

# How long a market ID stays in the dedup cache before it can be pushed again.
_DEDUP_TTL_SECONDS = 24 * 60 * 60  # 24 hours

# Default file path (relative to CWD where the scanner runs)
_DEFAULT_PATH = "pushed_markets.json"


class PushDedup:
    """File-based deduplication for Telegram push notifications.

    Parameters
    ----------
    path :
        Path to the JSON dedup file. Created automatically if absent.
    ttl_seconds :
        Time-to-live for each entry. After this many seconds, the market
        can be pushed again. Default: 24 hours.
    """

    def __init__(
        self,
        path: str = _DEFAULT_PATH,
        ttl_seconds: float = _DEDUP_TTL_SECONDS,
    ):
        self._path = Path(path)
        self._ttl = ttl_seconds
        self._cache: Dict[str, float] = {}
        self._load()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load cache from disk, purging stale entries."""
        if not self._path.exists():
            self._cache = {}
            return

        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read dedup file %s: %s — starting fresh", self._path, exc)
            self._cache = {}
            return

        # Purge stale entries
        now = time.time()
        self._cache = {
            mid: ts
            for mid, ts in raw.items()
            if isinstance(ts, (int, float)) and (now - ts) < self._ttl
        }

        purged = len(raw) - len(self._cache)
        if purged:
            log.debug("Dedup: purged %d stale entries (>%dh old)", purged, int(self._ttl / 3600))

    def _save(self) -> None:
        """Persist cache to disk."""
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
        except OSError as exc:
            log.warning("Could not write dedup file %s: %s", self._path, exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_already_pushed(self, market_id: str) -> bool:
        """Return True if this market was pushed within the TTL window."""
        return market_id in self._cache

    def filter_new(self, opportunities: List[Any]) -> List[Any]:
        """Return only opportunities whose market has NOT been pushed recently.

        Each opportunity must have an `opp.market.market_id` attribute
        (or be a tuple `(opp, decision)` from the scanner report).
        """
        result = []
        for item in opportunities:
            # Handle both raw opportunity and (opp, decision) tuples
            if isinstance(item, tuple):
                opp = item[0]
            else:
                opp = item
            mid = opp.market.market_id
            if not self.is_already_pushed(mid):
                result.append(item)
            else:
                log.debug("Dedup: skipping %s (pushed within %dh)", mid, int(self._ttl / 3600))
        return result

    def mark_pushed(self, opportunities: List[Any]) -> None:
        """Record these market IDs as pushed (with current timestamp).

        Accepts the same format as filter_new: raw opps or (opp, dec) tuples.
        """
        now = time.time()
        for item in opportunities:
            if isinstance(item, tuple):
                opp = item[0]
            else:
                opp = item
            self._cache[opp.market.market_id] = now

        self._save()
        log.debug("Dedup: marked %d markets as pushed", len(opportunities))

    @property
    def cache_size(self) -> int:
        """Number of market IDs currently in the dedup cache."""
        return len(self._cache)
