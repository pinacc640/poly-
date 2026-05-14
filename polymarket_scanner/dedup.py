"""推送去重模块 — 避免同一个市场短时间内重复推送。

在本地维护一个 JSON 文件记录已推送的市场 ID 和时间戳，
24 小时内推送过的市场不会再次推送。

用法
----
    from polymarket_scanner.dedup import PushDedup
    
    dedup = PushDedup()
    new_opps = dedup.filter_new(all_opportunities)  # 过滤已推送
    # ... 发送 new_opps ...
    dedup.mark_pushed(new_opps)  # 标记已推送
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Set

log = logging.getLogger(__name__)

DEFAULT_DEDUP_FILE = "push_history.json"
DEFAULT_TTL_HOURS = 24


class PushDedup:
    """推送去重管理器。"""

    def __init__(
        self,
        history_file: str = DEFAULT_DEDUP_FILE,
        ttl_hours: int = DEFAULT_TTL_HOURS,
    ):
        self.history_file = history_file
        self.ttl_seconds = ttl_hours * 3600
        self._history: Dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        """从文件加载推送历史。"""
        path = Path(self.history_file)
        if not path.exists():
            self._history = {}
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._history = {k: float(v) for k, v in raw.items()}
            log.debug("Loaded %d entries from push history", len(self._history))
        except (json.JSONDecodeError, OSError, TypeError) as e:
            log.warning("Could not load %s: %s — starting fresh", self.history_file, e)
            self._history = {}

    def _save(self) -> None:
        """保存推送历史到文件。"""
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(self._history, f, indent=2)
        except OSError as e:
            log.warning("Could not save %s: %s", self.history_file, e)

    def _cleanup_expired(self) -> None:
        """清理过期记录。"""
        now = time.time()
        expired = [k for k, v in self._history.items() if now - v > self.ttl_seconds]
        for k in expired:
            del self._history[k]
        if expired:
            log.debug("Cleaned up %d expired entries from push history", len(expired))

    def is_pushed(self, market_id: str) -> bool:
        """检查市场是否在 TTL 内已推送过。"""
        self._cleanup_expired()
        if market_id not in self._history:
            return False
        return time.time() - self._history[market_id] < self.ttl_seconds

    def get_pushed_ids(self) -> Set[str]:
        """获取所有未过期的已推送市场 ID。"""
        self._cleanup_expired()
        return set(self._history.keys())

    def filter_new(self, opportunities: List) -> List:
        """
        过滤掉已推送的机会。
        
        Parameters
        ----------
        opportunities : List
            机会对象列表（每个对象需要有 market_id 属性）
        
        Returns
        -------
        List
            未推送过的机会列表
        """
        self._cleanup_expired()
        pushed_ids = self.get_pushed_ids()

        new_opps = []
        for opp in opportunities:
            market_id = getattr(opp, "market_id", None) or getattr(opp, "id", None)
            if market_id and market_id not in pushed_ids:
                new_opps.append(opp)

        return new_opps

    def mark_pushed(self, opportunities: List) -> None:
        """
        标记机会为已推送。
        
        Parameters
        ----------
        opportunities : List
            刚刚推送的机会对象列表
        """
        now = time.time()
        for opp in opportunities:
            market_id = getattr(opp, "market_id", None) or getattr(opp, "id", None)
            if market_id:
                self._history[market_id] = now

        self._save()
        log.debug("Marked %d opportunities as pushed", len(opportunities))

    def clear(self) -> None:
        """清空所有推送历史。"""
        self._history = {}
        self._save()
        log.info("Push history cleared")
