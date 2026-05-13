"""Portfolio Sync — fetch on-chain positions from Polymarket Data API.

Uses the public Polymarket Data API (no HMAC auth required):
  GET https://data-api.polymarket.com/positions?user={address}

Writes results to a local portfolio.json file.

Environment Variables
---------------------
POLY_WALLET_ADDRESS : Signer / proxy address (0x…)
                      Default: hardcoded signer address below
POLY_API_KEY        : Optional Data API key for higher rate limits
                      Default: hardcoded key below

Usage
-----
    from polymarket_scanner.portfolio_sync import sync_portfolio, load_portfolio

    positions = sync_portfolio()       # fetches API → writes portfolio.json
    portfolio = load_portfolio()       # reads portfolio.json → dict
"""

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_API_URL    = "https://data-api.polymarket.com"
GAMMA_API_URL   = "https://gamma-api.polymarket.com"

# Signer address (from Polymarket settings → "Signer Address")
DEFAULT_WALLET  = "0x1139Fe3b54cf43A2AAD1E6E8C09aedf73E5270bf"

# Data API key (public/semi-public; set POLY_API_KEY env var to override)
DEFAULT_API_KEY = "019e2104-08f9-755c-ba70-404342688f5e"

PORTFOLIO_FILE  = "portfolio.json"
REQUEST_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Position:
    """A single active position in the portfolio."""
    market_id:      str
    question:       str    # market question (for display in alerts)
    side:           str    # "YES" or "NO"
    size:           float  # number of shares held
    avg_price:      float  # average entry price per share
    current_price:  float  # latest market price (refreshed each sync)
    unrealized_pnl: float  # (current_price - avg_price) * size  [YES side]

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _http_get(url: str, api_key: str = "", timeout: int = REQUEST_TIMEOUT) -> Optional[object]:
    """GET url → parsed JSON, or None on error."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "poly-scanner/1.0",
    }
    if api_key:
        headers["POLY_API_KEY"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log.warning("HTTP %s fetching %s: %s", exc.code, url, exc.reason)
    except urllib.error.URLError as exc:
        log.warning("URL error fetching %s: %s", url, exc.reason)
    except Exception as exc:  # pylint: disable=broad-except
        log.warning("Error fetching %s: %s", url, exc)
    return None


# ---------------------------------------------------------------------------
# API fetching
# ---------------------------------------------------------------------------

def _fetch_positions_data_api(wallet: str, api_key: str) -> List[dict]:
    """Primary: Data API GET /positions?user={address}

    Polymarket Data API (public, no HMAC):
      https://data-api.polymarket.com/positions?user=0x...
    Returns a list of position objects directly.
    """
    url = f"{DATA_API_URL}/positions?user={wallet}"
    log.debug("Data API positions: %s", url)
    data = _http_get(url, api_key=api_key)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("data") or data.get("positions") or []
    return []


def _fetch_positions_gamma(wallet: str) -> List[dict]:
    """Fallback: Gamma API GET /users/{address}/positions."""
    url = f"{GAMMA_API_URL}/users/{wallet}/positions"
    data = _http_get(url)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("positions") or data.get("data") or []
    return []


def _parse_position(raw: dict) -> Optional[Position]:
    """Parse a raw Data API position dict into our Position dataclass.

    Data API field reference (from live response inspection):
      asset          → token/outcome identifier
      conditionId    → market condition ID (our market_id)
      market         → market address (fallback id)
      question       → market question text
      outcome        → "YES" / "NO" (or index "0" / "1")
      size           → shares held (string or float)
      avgPrice       → average entry price (0..1)
      currentPrice   → latest price (0..1)
      cashBalance    → USDC value (alternative size field)
    """
    try:
        market_id = str(
            raw.get("conditionId") or
            raw.get("market") or
            raw.get("marketId") or
            raw.get("asset") or
            raw.get("id") or ""
        ).strip()
        if not market_id:
            return None

        question = str(
            raw.get("question") or
            raw.get("title") or
            raw.get("marketQuestion") or
            raw.get("market_slug") or ""
        )[:120]

        # side: "YES" / "NO"
        outcome = str(raw.get("outcome") or raw.get("side") or "").upper()
        side = "NO" if outcome in ("NO", "1") else "YES"

        # size — Data API returns float directly
        size = float(
            raw.get("size") or
            raw.get("shares") or
            raw.get("amount") or
            raw.get("tokensOwned") or 0
        )
        if size <= 0:
            return None

        # avg_price
        avg_price = float(
            raw.get("avgPrice") or
            raw.get("averagePrice") or
            raw.get("buyPrice") or
            raw.get("price") or 0
        )

        # current_price
        current_price = float(
            raw.get("currentPrice") or
            raw.get("curPrice") or
            raw.get("lastTradePrice") or
            raw.get("priceUsd") or
            avg_price
        )

        # unrealized P&L
        if side == "YES":
            unrealized_pnl = (current_price - avg_price) * size
        else:
            unrealized_pnl = ((1 - current_price) - (1 - avg_price)) * size

        return Position(
            market_id      = market_id,
            question       = question,
            side           = side,
            size           = round(size, 4),
            avg_price      = round(avg_price, 4),
            current_price  = round(current_price, 4),
            unrealized_pnl = round(unrealized_pnl, 4),
        )
    except (TypeError, ValueError) as exc:
        log.debug("Could not parse position %r: %s", raw, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sync_portfolio(
    wallet: Optional[str] = None,
    api_key: Optional[str] = None,
    output_path: str = PORTFOLIO_FILE,
) -> Dict[str, Position]:
    """Fetch on-chain positions and write to portfolio.json.

    Uses Polymarket Data API (no HMAC auth) as primary source,
    falls back to Gamma API if Data API returns empty.

    Parameters
    ----------
    wallet :
        Signer / proxy address. Falls back to env POLY_WALLET_ADDRESS
        or DEFAULT_WALLET.
    api_key :
        Data API key. Falls back to env POLY_API_KEY or DEFAULT_API_KEY.
    output_path :
        Path to the portfolio JSON file (overwritten on every call).

    Returns
    -------
    Dict mapping market_id → Position for all active positions.
    """
    wallet  = wallet  or os.getenv("POLY_WALLET_ADDRESS", DEFAULT_WALLET)
    api_key = api_key or os.getenv("POLY_API_KEY",        DEFAULT_API_KEY)
    log.info("Syncing portfolio for wallet %s…%s", wallet[:10], wallet[-4:])

    # ── Primary: Data API ──────────────────────────────────────────────────
    raw_list = _fetch_positions_data_api(wallet, api_key)
    source   = "Data API"

    # ── Fallback: Gamma API ────────────────────────────────────────────────
    if not raw_list:
        log.debug("Data API returned empty — trying Gamma API fallback…")
        raw_list = _fetch_positions_gamma(wallet)
        source   = "Gamma API"

    if not raw_list:
        log.warning(
            "No positions returned from Data API or Gamma API. "
            "Wallet %s may have no active positions.", wallet[-8:]
        )
        _write_portfolio({}, output_path)
        return {}

    portfolio: Dict[str, Position] = {}
    for raw in raw_list:
        pos = _parse_position(raw)
        if pos:
            portfolio[pos.market_id] = pos

    log.info(
        "Portfolio sync complete (%s): %d active position(s)",
        source, len(portfolio),
    )
    _write_portfolio(portfolio, output_path)
    return portfolio


def _write_portfolio(portfolio: Dict[str, Position], path: str) -> None:
    """Persist portfolio dict to JSON."""
    data = {mid: pos.to_dict() for mid, pos in portfolio.items()}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log.debug("portfolio.json written (%d positions)", len(data))
    except OSError as exc:
        log.warning("Could not write %s: %s", path, exc)


def load_portfolio(path: str = PORTFOLIO_FILE) -> Dict[str, Position]:
    """Load portfolio from JSON file previously written by sync_portfolio().

    Returns empty dict if file does not exist or is malformed.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {mid: Position(**data) for mid, data in raw.items()}
    except (json.JSONDecodeError, OSError, TypeError, KeyError) as exc:
        log.warning("Could not read %s: %s — starting with empty portfolio", path, exc)
        return {}
