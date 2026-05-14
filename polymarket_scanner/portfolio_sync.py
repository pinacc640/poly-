"""Portfolio Sync — fetch on-chain positions from Polymarket Data API.

Reads active positions for a given wallet address from the
Polymarket Data API, then writes them to a local portfolio.json file.

正确的 API 端点：
  https://data-api.polymarket.com/positions?user=0x你的地址

这是公开接口，无需任何认证，只要传钱包地址即可。

Environment Variables
---------------------
POLY_WALLET_ADDRESS : Your Polymarket wallet address (0x…)
                      可在 polymarket.com/settings 查看

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
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# 正确的 API 端点 — Data API（公开，无需认证）
DATA_API_URL    = "https://data-api.polymarket.com"
GAMMA_API_URL   = "https://gamma-api.polymarket.com"
DEFAULT_WALLET  = "0xbF5B386FCC49FFe6d1Fc3dA202cf8A799043Dc6b"
PORTFOLIO_FILE  = "portfolio.json"
REQUEST_TIMEOUT = 15


# ---------------------------------------------------------------------------
# .env 加载（不依赖 python-dotenv）
# ---------------------------------------------------------------------------
def _try_load_dotenv() -> None:
    """尝试从 .env 文件加载环境变量（不覆盖已存在的）。"""
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(__file__), "..", ".env"),
    ]
    for path in candidates:
        path = os.path.normpath(path)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, _, v = line.partition("=")
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and k not in os.environ:
                            os.environ[k] = v
                log.debug("Loaded .env from %s", path)
            except Exception as e:
                log.debug("Could not load .env from %s: %s", path, e)
            break


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

def _http_get(url: str, timeout: int = REQUEST_TIMEOUT) -> Optional[object]:
    """GET url → parsed JSON, or None on error."""
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "poly-scanner/2.0"},
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


# ---------------------------------------------------------------------------
# API fetching — 使用正确的 Data API
# ---------------------------------------------------------------------------

def _fetch_positions_data_api(wallet: str, timeout: int = REQUEST_TIMEOUT) -> List[dict]:
    """
    正确的持仓 API：
    GET https://data-api.polymarket.com/positions?user={address}
    
    这是公开接口，无需认证。
    """
    params = urllib.parse.urlencode({
        "user":          wallet,
        "sizeThreshold": "0.01",   # 过滤极小仓位
        "limit":         500,
        "sortBy":        "CURRENT",
        "sortDirection": "DESC",
    })
    url = f"{DATA_API_URL}/positions?{params}"
    log.debug("Fetching positions from: %s", url)
    
    data = _http_get(url, timeout)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("positions") or data.get("data") or []
    return []


def _fetch_gamma_market_info(condition_ids: List[str], timeout: int = REQUEST_TIMEOUT) -> Dict[str, dict]:
    """从 Gamma API 批量获取市场详情（流动性、成交量等）。"""
    if not condition_ids:
        return {}
    
    # Gamma API 限制批量查询数量
    params = urllib.parse.urlencode(
        [("conditionId", cid) for cid in condition_ids[:50]]
    )
    url = f"{GAMMA_API_URL}/markets?{params}"
    
    data = _http_get(url, timeout)
    if not data:
        return {}
    
    raw_list = data if isinstance(data, list) else (data.get("data") or data.get("markets") or [])
    
    mapping: Dict[str, dict] = {}
    for item in raw_list:
        cid = str(item.get("conditionId") or item.get("id") or "").strip()
        if cid:
            mapping[cid] = item
    return mapping


def _parse_position(raw: dict, gamma_info: Optional[dict] = None) -> Optional[Position]:
    """Parse a raw API position dict into our Position dataclass."""
    try:
        # market_id: 优先用 conditionId
        market_id = str(
            raw.get("conditionId") or
            raw.get("market") or
            raw.get("marketId") or
            raw.get("id") or ""
        ).strip()
        if not market_id:
            return None

        # question
        question = str(
            raw.get("title") or
            raw.get("question") or
            raw.get("marketQuestion") or ""
        )[:120]
        
        # 如果 Data API 没有 title，从 Gamma 补充
        if not question and gamma_info:
            question = str(gamma_info.get("question") or gamma_info.get("title") or "")[:120]

        # side: "YES" / "NO"
        outcome = str(raw.get("outcome") or raw.get("side") or "Yes").lower()
        side = "NO" if outcome in ("no", "1") else "YES"

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

        # current_price
        current_price = float(
            raw.get("curPrice") or
            raw.get("currentPrice") or
            raw.get("lastTradePrice") or
            avg_price
        )
        
        # 如果有 Gamma 数据，用更准确的价格
        if gamma_info:
            outcomes = gamma_info.get("outcomePrices") or []
            if isinstance(outcomes, list) and outcomes:
                idx = 1 if side == "NO" and len(outcomes) >= 2 else 0
                if len(outcomes) > idx:
                    current_price = float(outcomes[idx])

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
) -> Dict[str, Position]:
    """Fetch on-chain positions and write to portfolio.json.

    使用正确的 Data API：
    GET https://data-api.polymarket.com/positions?user={address}

    Parameters
    ----------
    wallet :
        Polymarket wallet address. Falls back to env POLY_WALLET_ADDRESS
        or the hardcoded default address.
    output_path :
        Path to the portfolio JSON file (overwritten on every call).

    Returns
    -------
    Dict mapping market_id → Position for all active positions.
    """
    # 先加载 .env
    _try_load_dotenv()
    
    wallet = wallet or os.getenv("POLY_WALLET_ADDRESS", DEFAULT_WALLET)
    log.info("Syncing portfolio for wallet %s…%s", wallet[:10], wallet[-4:])

    # 使用正确的 Data API
    raw_list = _fetch_positions_data_api(wallet)

    if not raw_list:
        log.warning(
            "No positions returned from Data API — "
            "portfolio may be empty or wallet has no active positions."
        )
        _write_portfolio({}, output_path)
        return {}

    log.info("Data API returned %d position(s)", len(raw_list))

    # 按 conditionId 去重，每个市场只保留 size 最大的记录
    seen = {}
    for raw in raw_list:
        cid = str(raw.get("conditionId") or raw.get("market") or "").strip()
        if not cid:
            continue
        existing = seen.get(cid)
        if existing is None or float(raw.get("size") or 0) > float(existing.get("size") or 0):
            seen[cid] = raw
    raw_list = list(seen.values())
    log.info("After dedup: %d unique position(s)", len(raw_list))

    # 批量查 Gamma 补充数据
    condition_ids = [str(p.get("conditionId") or "").strip() for p in raw_list if p.get("conditionId")]
    gamma_map = _fetch_gamma_market_info(condition_ids)
    if gamma_map:
        log.info("Gamma API supplemented %d/%d market(s)", len(gamma_map), len(condition_ids))

    portfolio: Dict[str, Position] = {}
    for raw in raw_list:
        cid = str(raw.get("conditionId") or "").strip()
        gamma_info = gamma_map.get(cid)
        pos = _parse_position(raw, gamma_info)
        if pos:
            portfolio[pos.market_id] = pos

    log.info("Portfolio sync complete: %d active position(s)", len(portfolio))
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
