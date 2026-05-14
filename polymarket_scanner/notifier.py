"""Telegram 通知模块 — 将交易机会推送到 Telegram。

环境变量
--------
TELEGRAM_BOT_TOKEN : Telegram Bot Token（从 @BotFather 获取）
TELEGRAM_CHAT_ID   : 目标聊天 ID（你的用户 ID 或群组 ID）

用法
----
    from polymarket_scanner.notifier import TelegramNotifier
    
    notifier = TelegramNotifier()
    if notifier.is_enabled():
        notifier.send_report(scan_report)
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

log = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    """Telegram 消息推送器。"""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout: int = 10,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.timeout = timeout
        self._enabled = bool(self.bot_token and self.chat_id)

        if not self._enabled:
            log.info(
                "TelegramNotifier: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — "
                "notifications disabled."
            )
        else:
            # 打印 token 前10位方便调试（确认读到了正确的值）
            log.debug("TelegramNotifier: token=%s... chat_id=%s",
                      self.bot_token[:10], self.chat_id)
            # 验证 token 是否有效
            try:
                test_url = f"{TELEGRAM_API_BASE}/bot{self.bot_token}/getMe"
                req = urllib.request.Request(test_url)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    if not result.get("ok"):
                        log.warning("Telegram Bot Token 无效（getMe 返回 false）— 推送已禁用。请重新生成 token。")
                        self._enabled = False
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    log.warning("Telegram Bot Token 已失效（401 Unauthorized）— 推送已禁用。请到 @BotFather 重新生成 token。")
                    self._enabled = False
            except Exception:
                pass  # 网络问题不影响其他功能

    def is_enabled(self) -> bool:
        """检查是否已配置 Telegram。"""
        return self._enabled

    def _post(self, text: str, parse_mode: str = "HTML") -> bool:
        """发送单条消息。"""
        if not self._enabled:
            return False

        url = f"{TELEGRAM_API_BASE}/bot{self.bot_token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("ok", False)
        except urllib.error.HTTPError as e:
            log.warning("Telegram HTTP %d: %s", e.code, e.reason)
        except urllib.error.URLError as e:
            log.warning("Telegram 连接失败: %s", e.reason)
        except Exception as e:
            log.warning("Telegram 发送错误: %s", e)
        return False

    def send_text(self, text: str) -> bool:
        """发送纯文本消息。"""
        return self._post(text)

    def _format_opportunity(self, opp, strategy_name: str) -> str:
        """格式化单个交易机会为 HTML。"""
        market_id = getattr(opp, "market_id", "N/A")
        question = getattr(opp, "question", "Unknown")[:80]
        side = getattr(opp, "side", "?")
        kelly_bet = getattr(opp, "kelly_bet", 0)
        expected_profit = getattr(opp, "expected_profit", 0)
        entry_price = getattr(opp, "entry_price", 0)
        take_profit = getattr(opp, "take_profit_price", 0)
        true_prob = getattr(opp, "true_prob", 0)

        return (
            f"<b>🎯 {strategy_name}</b>\n"
            f"<b>Market:</b> {question}\n"
            f"<b>ID:</b> <code>{market_id}</code>\n"
            f"<b>Side:</b> {side} @ {entry_price:.4f}\n"
            f"<b>AI Prob:</b> {true_prob:.1%}\n"
            f"<b>Bet:</b> ${kelly_bet:.2f}\n"
            f"<b>Expected:</b> +${expected_profit:.2f}\n"
            f"<b>TP:</b> {take_profit:.4f}"
        )

    def send_report(self, report) -> int:
        """
        发送扫描报告中的所有交易机会。
        
        Parameters
        ----------
        report : ScanReport
            扫描报告对象
        
        Returns
        -------
        int
            成功发送的消息数量
        """
        if not self._enabled:
            return 0

        sent = 0
        all_opps = []

        # 收集所有机会
        for opp in getattr(report, "stable_approved", []):
            all_opps.append((opp, "Stable"))
        for opp in getattr(report, "volatility_approved", []):
            all_opps.append((opp, "Volatility"))
        for opp in getattr(report, "smart_money_approved", []):
            all_opps.append((opp, "Smart Money"))
        for opp in getattr(report, "arbitrage_found", []):
            all_opps.append((opp, "Arbitrage"))

        if not all_opps:
            return 0

        # 发送汇总
        header = (
            f"📊 <b>Polymarket Scanner Alert</b>\n"
            f"Found {len(all_opps)} opportunity(ies)\n"
            f"{'─' * 30}"
        )
        if self._post(header):
            sent += 1

        # 发送每个机会
        for opp, strategy in all_opps:
            msg = self._format_opportunity(opp, strategy)
            if self._post(msg):
                sent += 1

        return sent

    def send_opportunities(self, opportunities: List, strategy_name: str = "Signal") -> int:
        """
        发送机会列表。
        
        Parameters
        ----------
        opportunities : List
            机会对象列表
        strategy_name : str
            策略名称
        
        Returns
        -------
        int
            成功发送的消息数量
        """
        if not self._enabled or not opportunities:
            return 0

        sent = 0
        for opp in opportunities:
            msg = self._format_opportunity(opp, strategy_name)
            if self._post(msg):
                sent += 1

        return sent
