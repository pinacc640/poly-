"""基本面检查模块 — 用 DeepSeek 做最后一道 sanity check。

在推送前对每个机会做一次快速的基本面审核，过滤掉明显不合理的信号。

环境变量
--------
DEEPSEEK_API_KEY : DeepSeek API Key

用法
----
    from polymarket_scanner.fundamentals import FundamentalsChecker
    
    checker = FundamentalsChecker()
    if checker.is_available():
        verified = checker.check_opportunities(opportunities)
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import List, Optional

log = logging.getLogger(__name__)

DEEPSEEK_API_BASE = "https://api.deepseek.com/v1"


class FundamentalsChecker:
    """DeepSeek 基本面检查器。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "deepseek-chat",
        timeout: int = 30,
    ):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self.model = model
        self.timeout = timeout
        self._available = bool(self.api_key)

    def is_available(self) -> bool:
        """检查是否已配置 API Key。"""
        return self._available

    def _call_deepseek(self, prompt: str) -> Optional[str]:
        """调用 DeepSeek API。"""
        if not self._available:
            return None

        url = f"{DEEPSEEK_API_BASE}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是一个预测市场分析师。用户会给你一个交易机会的描述，"
                        "你需要快速判断这个信号是否合理。回复 JSON 格式：\n"
                        '{"pass": true/false, "reason": "简短理由"}'
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 100,
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"]
        except Exception as e:
            log.warning("DeepSeek 调用失败: %s", e)
            return None

    def check_single(self, opp) -> bool:
        """
        检查单个机会是否通过基本面审核。
        
        Parameters
        ----------
        opp : object
            机会对象
        
        Returns
        -------
        bool
            True 表示通过，False 表示被过滤
        """
        if not self._available:
            return True  # 没配置就默认通过

        question = getattr(opp, "question", "Unknown market")
        side = getattr(opp, "side", "?")
        true_prob = getattr(opp, "true_prob", 0)
        price = getattr(opp, "entry_price", 0) or getattr(opp, "price", 0)
        expected_profit = getattr(opp, "expected_profit", 0)

        prompt = (
            f"市场问题: {question}\n"
            f"交易方向: {side}\n"
            f"当前价格: {price:.4f}\n"
            f"AI 估计概率: {true_prob:.1%}\n"
            f"预期利润: ${expected_profit:.2f}\n\n"
            f"这个交易信号是否合理？价格和概率估计是否有明显矛盾？"
        )

        response = self._call_deepseek(prompt)
        if not response:
            return True  # API 失败默认通过

        try:
            # 尝试解析 JSON
            result = json.loads(response)
            passed = result.get("pass", True)
            reason = result.get("reason", "")
            if not passed:
                log.info("❌ Fundamentals rejected: %s — %s", question[:50], reason)
            return passed
        except json.JSONDecodeError:
            # 解析失败，尝试从文本中判断
            response_lower = response.lower()
            if "不合理" in response or "false" in response_lower or "reject" in response_lower:
                log.info("❌ Fundamentals rejected: %s — %s", question[:50], response[:100])
                return False
            return True

    def check_opportunities(self, opportunities: List) -> List:
        """
        批量检查机会列表。
        
        Parameters
        ----------
        opportunities : List
            机会对象列表
        
        Returns
        -------
        List
            通过检查的机会列表
        """
        if not self._available:
            return opportunities

        verified = []
        for opp in opportunities:
            if self.check_single(opp):
                verified.append(opp)

        log.info("Fundamentals check: %d/%d passed", len(verified), len(opportunities))
        return verified
