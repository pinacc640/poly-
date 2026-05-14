"""Gamma API 客户端 — 从 Polymarket Gamma API 拉取市场数据。

Gamma API 是 Polymarket 的公开市场数据接口，无需认证。
文档：https://gamma-api.polymarket.com

用法
----
    from polymarket_scanner.gamma_client import GammaClient
    
    client = GammaClient()
    if client.health_check():
        markets = client.fetch_active_markets(limit=500)
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DEFAULT_TIMEOUT = 15


class GammaClient:
    """Polymarket Gamma API 客户端。"""

    def __init__(self, base_url: str = GAMMA_BASE_URL, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, endpoint: str, params: Optional[dict] = None) -> Optional[object]:
        """发送 GET 请求，返回解析后的 JSON。"""
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
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            log.warning("Gamma API HTTP %d: %s (%s)", e.code, e.reason, endpoint)
        except urllib.error.URLError as e:
            log.warning("Gamma API 连接失败: %s (%s)", e.reason, endpoint)
        except Exception as e:
            log.warning("Gamma API 错误: %s (%s)", e, endpoint)
        return None

    def health_check(self) -> bool:
        """检查 Gamma API 是否可达。"""
        try:
            # 尝试拉取 1 条市场数据
            data = self._request("/markets", {"limit": 1, "active": "true"})
            return isinstance(data, list) and len(data) > 0
        except Exception:
            return False

    def fetch_active_markets(self, limit: int = 500) -> List[dict]:
        """
        拉取活跃市场列表。
        
        Parameters
        ----------
        limit : int
            最多返回多少条记录（Gamma API 单次最大 500）
        
        Returns
        -------
        List[dict]
            原始市场数据列表
        """
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
            log.debug("Fetched %d markets (offset=%d)", len(data), offset)

            if len(data) < batch_size:
                # 没有更多数据了
                break

            offset += batch_size

        log.info("Fetched %d active markets from Gamma API.", len(all_markets))
        return all_markets[:limit]

    def fetch_market_by_id(self, market_id: str) -> Optional[dict]:
        """根据 ID 获取单个市场详情。"""
        data = self._request(f"/markets/{market_id}")
        if isinstance(data, dict):
            return data
        return None

    def fetch_markets_by_ids(self, market_ids: List[str]) -> List[dict]:
        """批量获取市场详情。"""
        if not market_ids:
            return []
        
        # Gamma API 支持批量查询
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
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data if isinstance(data, list) else []
        except Exception as e:
            log.warning("批量查询失败: %s", e)
            return []
