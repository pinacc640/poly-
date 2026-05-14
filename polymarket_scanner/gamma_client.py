"""Gamma API 客户端 — 从 Polymarket Gamma API 拉取市场数据。

Gamma API 是 Polymarket 的公开市场数据接口，无需认证。
文档：https://gamma-api.polymarket.com
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import ssl
from typing import List, Optional

log = logging.getLogger(__name__)

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DEFAULT_TIMEOUT = 15


class GammaClient:
    """Polymarket Gamma API 客户端。"""

    def __init__(self, base_url: str = GAMMA_BASE_URL, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # 宽松 SSL 上下文（解决 Python 3.13 证书验证问题）
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def _request(self, endpoint: str, params: Optional[dict] = None) -> Optional[object]:
        url = f"{self.base_url}{endpoint}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "polymarket-scanner/2.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            log.warning("Gamma API HTTP %d: %s (%s)", e.code, e.reason, endpoint)
        except urllib.error.URLError as e:
            log.warning("Gamma API 连接失败: %s (%s)", e.reason, endpoint)
        except Exception as e:
            log.warning("Gamma API 错误: %s (%s)", e, endpoint)
        return None

    def health_check(self) -> bool:
        try:
            data = self._request("/markets", {"limit": 1, "active": "true"})
            return isinstance(data, list) and len(data) > 0
        except Exception:
            return False

    def fetch_active_markets(self, limit: int = 500) -> List[dict]:
        all_markets: List[dict] = []
        offset = 0
        batch_size = min(limit, 500)

        while len(all_markets) < limit:
            params = {
                "active": "true",
                "closed": "false",
                "limit": batch_size,
                "offset": offset,
            }
            data = self._request("/markets", params)

            if not data or not isinstance(data, list):
                break

            all_markets.extend(data)

            if len(data) < batch_size:
                break

            offset += batch_size

        log.info("Fetched %d active markets from Gamma API.", len(all_markets))
        return all_markets[:limit]

    def fetch_market_by_id(self, market_id: str) -> Optional[dict]:
        data = self._request(f"/markets/{market_id}")
        if isinstance(data, dict):
            return data
        return None

    def fetch_markets_by_ids(self, market_ids: List[str]) -> List[dict]:
        if not market_ids:
            return []
        
        params_list = [("id", mid) for mid in market_ids[:50]]
        params_str = urllib.parse.urlencode(params_list)
        url = f"{self.base_url}/markets?{params_str}"
        
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "polymarket-scanner/2.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data if isinstance(data, list) else []
        except Exception as e:
            log.warning("批量查询失败: %s", e)
            return []