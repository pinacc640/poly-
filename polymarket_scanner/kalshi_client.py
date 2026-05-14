"""Kalshi API 客户端 — 从 Kalshi 拉取市场数据用于跨平台套利。

Kalshi 是另一个预测市场平台，与 Polymarket 类似但在美国合规运营。
通过比较两个平台同一事件的价格差，可以发现套利机会。

API 文档：https://trading-api.readme.io/reference/

用法
----
    from polymarket_scanner.kalshi_client import KalshiClient
    
    client = KalshiClient()
    if client.health_check():
        markets = client.fetch_active_markets(limit=100)
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_TIMEOUT = 15


class KalshiClient:
    """Kalshi API 客户端。"""

    def __init__(
        self,
        base_url: str = KALSHI_API_BASE,
        timeout: int = DEFAULT_TIMEOUT,
    ):
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
            log.warning("Kalshi API HTTP %d: %s (%s)", e.code, e.reason, endpoint)
        except urllib.error.URLError as e:
            log.warning("Kalshi API 连接失败: %s (%s)", e.reason, endpoint)
        except Exception as e:
            log.warning("Kalshi API 错误: %s (%s)", e, endpoint)
        return None

    def health_check(self) -> bool:
        """检查 Kalshi API 是否可达。"""
        try:
            data = self._request("/markets", {"limit": 1, "status": "open"})
            if isinstance(data, dict):
                markets = data.get("markets", [])
                return isinstance(markets, list)
            return False
        except Exception:
            return False

    def fetch_active_markets(self, limit: int = 200) -> List[dict]:
        """
        拉取活跃市场列表。
        
        Parameters
        ----------
        limit : int
            最多返回多少条记录
        
        Returns
        -------
        List[dict]
            市场数据列表
        """
        all_markets: List[dict] = []
        cursor = None

        while len(all_markets) < limit:
            params = {
                "limit": min(100, limit - len(all_markets)),
                "status": "open",
            }
            if cursor:
                params["cursor"] = cursor

            data = self._request("/markets", params)

            if not isinstance(data, dict):
                break

            markets = data.get("markets", [])
            if not markets:
                break

            # 只保留单一结果的市场（二元市场）
            for m in markets:
                if self._is_binary_market(m):
                    all_markets.append(m)

            cursor = data.get("cursor")
            if not cursor:
                break

        log.info("Fetched %d active single-outcome markets from Kalshi.", len(all_markets))
        return all_markets[:limit]

    def _is_binary_market(self, market: dict) -> bool:
        """判断是否为二元市场（只有 Yes/No 两种结果）。"""
        # Kalshi 的二元市场通常只有一个 ticker
        # 多结果市场会有多个相关 ticker
        return True  # 简化处理，后续可以细化

    def get_market_price(self, market: dict) -> Dict[str, float]:
        """
        从市场数据中提取价格。
        
        Returns
        -------
        Dict with keys: yes_bid, yes_ask, no_bid, no_ask
        """
        yes_bid = market.get("yes_bid", 0) / 100 if market.get("yes_bid") else 0
        yes_ask = market.get("yes_ask", 0) / 100 if market.get("yes_ask") else 0
        no_bid = market.get("no_bid", 0) / 100 if market.get("no_bid") else 0
        no_ask = market.get("no_ask", 0) / 100 if market.get("no_ask") else 0

        # Kalshi 价格是 cents，需要除以 100
        return {
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "yes_mid": (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else 0,
            "no_mid": (no_bid + no_ask) / 2 if no_bid and no_ask else 0,
        }

    def match_polymarket(
        self,
        kalshi_markets: List[dict],
        poly_markets: List,
    ) -> List[dict]:
        """
        匹配 Kalshi 和 Polymarket 的相似市场。
        
        使用简单的关键词匹配。更精确的匹配需要 NLP 或手动映射。
        
        Returns
        -------
        List of dicts with keys: kalshi, poly, spread
        """
        matches = []

        # 建立 Polymarket 市场的关键词索引
        poly_index: Dict[str, List] = {}
        for pm in poly_markets:
            question = getattr(pm, "question", "").lower()
            # 提取关键词
            keywords = self._extract_keywords(question)
            for kw in keywords:
                if kw not in poly_index:
                    poly_index[kw] = []
                poly_index[kw].append(pm)

        # 对每个 Kalshi 市场寻找匹配
        for km in kalshi_markets:
            title = km.get("title", "").lower()
            keywords = self._extract_keywords(title)

            # 找最佳匹配
            best_match = None
            best_score = 0

            for kw in keywords:
                for pm in poly_index.get(kw, []):
                    score = self._match_score(km, pm)
                    if score > best_score:
                        best_score = score
                        best_match = pm

            if best_match and best_score >= 2:  # 至少 2 个关键词匹配
                kalshi_price = self.get_market_price(km)
                poly_price = getattr(best_match, "price", 0.5)

                # 计算价差
                spread = abs(kalshi_price["yes_mid"] - poly_price)

                matches.append({
                    "kalshi": km,
                    "poly": best_match,
                    "kalshi_yes": kalshi_price["yes_mid"],
                    "poly_yes": poly_price,
                    "spread": spread,
                })

        # 按价差排序
        matches.sort(key=lambda x: x["spread"], reverse=True)
        return matches

    def _extract_keywords(self, text: str) -> List[str]:
        """从文本中提取关键词。"""
        # 简单的关键词提取
        stopwords = {
            "the", "a", "an", "is", "are", "will", "be", "to", "of", "in", "on",
            "by", "for", "at", "or", "and", "if", "it", "as", "than", "that",
            "this", "what", "who", "when", "where", "how", "yes", "no",
        }
        words = text.lower().split()
        keywords = [
            w.strip("?.,!\"'()[]{}") 
            for w in words 
            if len(w) > 2 and w not in stopwords
        ]
        return keywords

    def _match_score(self, kalshi: dict, poly) -> int:
        """计算两个市场的匹配分数。"""
        k_title = kalshi.get("title", "").lower()
        p_question = getattr(poly, "question", "").lower()

        k_keywords = set(self._extract_keywords(k_title))
        p_keywords = set(self._extract_keywords(p_question))

        return len(k_keywords & p_keywords)
