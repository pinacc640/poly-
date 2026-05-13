"""Portfolio Sync — fetch on-chain positions from Polymarket CLOB API.

Reads active positions for a given wallet address (Proxy Address) from the
Polymarket CLOB API, then writes them to a local portfolio.json file.

Environment Variables
---------------------
POLY_WALLET_ADDRESS : Your Polymarket Proxy Address (0x…)
                      Default: the hardcoded address passed to sync_portfolio()

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
CLOB_API_URL      = "https://clob.polymarket.com"
GAMMA_API_URL     = "https://gamma-api.polymarket.com"
DEFAULT_WALLET    = "0xbF5B386FCC49FFe6d1Fc3dA202cF8A799043Dc6b"
PORTFOLIO_FILE    = "portfolio.json"
REQUEST_TIMEOUT   = 15


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
    unrealized_pnl: float  # (current_price - avg_price) * size  [for YES side]

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# API fetching
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = REQUEST_TIMEOUT) -> Optional[object]:
    """GET url → parsed JSON, or None on error."""
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "poly-scanner/1.0"},
    )
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


def _fetch_positions_gamma(wallet: str) -> List[dict]:
    """Try Gamma API: GET /users/{address}/positions."""
    data = _http_get(f"{GAMMA_API_URL}/users/{wallet}/positions")
    if isinstance(data, list):
        return data
    # Some endpoints wrap in {"positions": [...]}
    if isinstance(data, dict):
        return data.get("positions") or data.get("data") or []
    return []


def _fetch_positions_clob(wallet: str) -> List[dict]:
    """Try CLOB API: GET /positions?user={address}."""
    data = _http_get(f"{CLOB_API_URL}/positions?user={wallet}")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("data") or data.get("positions") or []
    return []


def _parse_position(raw: dict) -> Optional[Position]:
    """Parse a raw API position dict into our Position dataclass."""
    try:
        # market_id: try several field names
        market_id = str(
            raw.get("conditionId") or
            raw.get("market") or
            raw.get("marketId") or
            raw.get("id") or ""
        ).strip()
        if not market_id:
            return None

        question = str(
            raw.get("question") or
            raw.get("title") or
            raw.get("marketQuestion") or ""
        )[:120]

        # side: "YES" / "NO"
        outcome = str(raw.get("outcome") or raw.get("side") or "").upper()
        if outcome in ("NO", "1"):
            side = "NO"
        else:
            side = "YES"

        # size
        size = float(raw.get("size") or raw.get("shares") or raw.get("amount") or 0)
        if size <= 0:
            return None

        # avg_price
        avg_price = float(
            raw.get("avgPrice") or
            raw.get("averagePrice") or
            raw.get("price") or 0
        )

        # current_price (may not be available; default to avg)
        current_price = float(
            raw.get("currentPrice") or
            raw.get("curPrice") or
            raw.get("lastTradePrice") or
            avg_price
        )

        # unrealized P&L
        if side == "YES":
            unrealized_pnl = (current_price - avg_price) * size
        else:
            # NO side: profit when YES price falls
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
    output_path: str = PORTFOLIO_FILE,
) -> Dict[str, "Position"]:
    """Fetch on-chain positions and write to portfolio.json.

    Tries Gamma API first; falls back to CLOB API.

    Parameters
    ----------
    wallet :
        Polymarket Proxy Address. Falls back to env POLY_WALLET_ADDRESS
        or the hardcoded default address.
    output_path :
        Path to the portfolio JSON file (overwritten on every call).

    Returns
    -------
    Dict mapping market_id → Position for all active positions.
    """
    wallet = wallet or os.getenv("POLY_WALLET_ADDRESS", DEFAULT_WALLET)
    log.info("Syncing portfolio for wallet %s…%s", wallet[:10], wallet[-4:])

    # Try Gamma API first (richer market data)
    raw_list = _fetch_positions_gamma(wallet)
    source = "Gamma"

    if not raw_list:
        raw_list = _fetch_positions_clob(wallet)
        source = "CLOB"

    if not raw_list:
        log.warning("No positions returned from either API — portfolio may be empty or wallet has no positions.")
        _write_portfolio({}, output_path)
        return {}

    portfolio: Dict[str, Position] = {}
    for raw in raw_list:
        pos = _parse_position(raw)
        if pos:
            portfolio[pos.market_id] = pos

    log.info("Portfolio sync complete (%s): %d active position(s)", source, len(portfolio))
    _write_portfolio(portfolio, output_path)
    return portfolio


def _write_portfolio(portfolio: Dict[str, "Position"], path: str) -> None:
    """Persist portfolio dict to JSON."""
    data = {mid: pos.to_dict() for mid, pos in portfolio.items()}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log.debug("portfolio.json written (%d positions)", len(data))
    except OSError as exc:
        log.warning("Could not write %s: %s", path, exc)


def load_portfolio(path: str = PORTFOLIO_FILE) -> Dict[str, "Position"]:
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
