"""arbitrage_scanner.py — Polymarket × Kalshi 跨平台套利扫描 (Phase 2)

套利逻辑
────────
若同一事件在两个平台的定价之和 < 1，则存在无风险利润空间：

    套利 = 买 PM YES(p₁) + 买 Kalshi NO(p₂)
    条件 = p₁ + p₂ < 1
    利润 = 1 - p₁ - p₂  （arb_gap）

或反向套利：

    套利 = 买 PM NO(1-p₁) + 买 Kalshi YES(p₂)
    条件 = (1-p₁) + p₂ < 1  →  p₁ > p₂ 时成立

两种方向都会被扫描，取 arb_gap 更大的方向。

市场匹配策略（两阶段）
──────────────────────
Stage 1 – Jaccard 关键词初筛（零 API 消耗）
    提取问题标题的关键词集合，计算 Jaccard 相似度。
    similarity = |A ∩ B| / |A ∪ B|
    低于 cfg.arb_jaccard_threshold（默认 0.30）直接丢弃。

Stage 2 – DeepSeek AI 等价验证（只对候选对执行）
    发送一条简短 prompt："这两个市场是否描述同一事件？"
    DeepSeek 返回 yes / no / uncertain。
    match_confidence：
      "yes"       → 0.95
      "uncertain" → 0.50
      "no"        → 0.05（从候选中移除）
    最多验证 cfg.arb_ai_verify_top_n（默认 10）对（按 Jaccard 分数排序）。

环境变量
────────
KALSHI_API_KEY   (可选) — 有 key 时走认证端点；无 key 时走公开端点
                          Kalshi 公开市场列表无需 key。

Kalshi API 参考端点
───────────────────
GET https://api.elections.kalshi.com/trade-api/v2/markets
    ?status=open&limit=200&cursor=<next_cursor>
字段：ticker, title, yes_bid, yes_ask, volume, open_interest, close_time

用法
────
    from polymarket_scanner.arbitrage_scanner import ArbitrageScanner
    scanner = ArbitrageScanner(cfg, deepseek_key="sk-...", brave_key="...")
    report  = scanner.scan(pm_markets)
    print(format_arb_report(report))
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Set, Tuple

from .config import AccountConfig, DEFAULT_CONFIG
from .models import ArbitrageOpportunity, ArbitrageReport, KalshiMarket, Market

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kalshi API 常量
# ---------------------------------------------------------------------------
KALSHI_API_BASE  = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_MARKETS   = f"{KALSHI_API_BASE}/markets"
REQUEST_TIMEOUT  = 15
RETRY_ATTEMPTS   = 2

# 停用词：计算 Jaccard 时忽略这些词
_STOPWORDS: Set[str] = {
    "will", "the", "a", "an", "of", "in", "on", "at", "to", "be",
    "is", "are", "was", "were", "by", "for", "with", "this", "that",
    "it", "or", "and", "if", "do", "does", "did", "has", "have",
    "had", "who", "what", "when", "where", "which", "how", "yes", "no",
    "win", "lose", "get", "make", "take", "come", "go", "2024", "2025",
    "2026", "than", "more", "least", "most", "any", "new", "next",
}


# ---------------------------------------------------------------------------
# Kalshi 数据获取
# ---------------------------------------------------------------------------

class KalshiFetcher:
    """从 Kalshi 公开 API 拉取活跃市场列表。

    Parameters
    ----------
    api_key :
        Kalshi API Key（可选）。若不提供，则使用公开端点（无认证）。
    timeout :
        HTTP 请求超时秒数。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = REQUEST_TIMEOUT,
    ):
        self.api_key = api_key or os.getenv("KALSHI_API_KEY", "")
        self.timeout = timeout

    def _make_headers(self) -> dict:
        h = {"Accept": "application/json", "User-Agent": "polymarket-scanner/1.0"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _fetch_page(self, cursor: Optional[str], limit: int = 200) -> dict:
        """拉取单页市场数据，返回原始 JSON dict。"""
        params: dict = {"status": "open", "limit": limit}
        if cursor:
            params["cursor"] = cursor
        url = f"{KALSHI_MARKETS}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=self._make_headers())

        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                logger.warning("Kalshi API HTTP %s (attempt %d): %s", exc.code, attempt, exc.reason)
                if exc.code in {401, 403}:
                    logger.warning("Kalshi 认证失败，请检查 KALSHI_API_KEY 环境变量。")
                    return {}
            except urllib.error.URLError as exc:
                logger.warning("Kalshi API 连接失败 (attempt %d): %s", attempt, exc.reason)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("Kalshi API 未知错误 (attempt %d): %s", attempt, exc)
            if attempt < RETRY_ATTEMPTS:
                time.sleep(1)
        return {}

    @staticmethod
    def _parse_market(raw: dict) -> Optional[KalshiMarket]:
        """把 Kalshi API 返回的单条 market dict 解析为 KalshiMarket。"""
        try:
            ticker   = str(raw.get("ticker") or "").strip()
            question = str(raw.get("title")  or raw.get("question") or "").strip()
            if not ticker or not question:
                return None

            # Kalshi 价格单位是分（0–99），除以 100 得到 0–1
            yes_bid = float(raw.get("yes_bid") or 0) / 100
            yes_ask = float(raw.get("yes_ask") or 0) / 100
            yes_price = (yes_bid + yes_ask) / 2 if (yes_bid + yes_ask) > 0 else (
                float(raw.get("last_price") or 50) / 100
            )
            yes_price = max(0.01, min(0.99, yes_price))
            no_price  = 1.0 - yes_price

            volume_usd    = float(raw.get("volume_24h") or raw.get("volume") or 0)
            open_interest = float(raw.get("open_interest") or 0)
            category      = str(raw.get("category") or "general").lower()
            close_time    = raw.get("close_time") or raw.get("expiration_time")

            return KalshiMarket(
                ticker        = ticker,
                question      = question,
                yes_price     = round(yes_price, 4),
                no_price      = round(no_price,  4),
                volume_usd    = volume_usd,
                open_interest = open_interest,
                category      = category,
                close_time    = close_time,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("Kalshi market 解析跳过: %s — raw=%s", exc, raw)
            return None

    def fetch_markets(self, max_markets: int = 500) -> List[KalshiMarket]:
        """拉取 Kalshi 所有活跃市场（自动翻页），返回 KalshiMarket 列表。"""
        all_markets: List[KalshiMarket] = []
        cursor: Optional[str] = None
        page_size = min(200, max_markets)

        while len(all_markets) < max_markets:
            data = self._fetch_page(cursor, limit=page_size)
            if not data:
                break

            raw_list = data.get("markets") or []
            for raw in raw_list:
                m = self._parse_market(raw)
                if m is not None:
                    all_markets.append(m)

            cursor = data.get("cursor")
            if not cursor or not raw_list:
                break

        logger.info("Kalshi: 已获取 %d 个活跃市场", len(all_markets))
        return all_markets[:max_markets]


# ---------------------------------------------------------------------------
# 文本工具（Jaccard 相似度）
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> Set[str]:
    """小写化 → 去标点 → 去停用词，返回词集合。"""
    words = re.findall(r"[a-z]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) >= 3}


def _jaccard(a: str, b: str) -> float:
    """计算两个问题标题的 Jaccard 相似度。"""
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    return len(inter) / len(union)


# ---------------------------------------------------------------------------
# DeepSeek 等价验证
# ---------------------------------------------------------------------------

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

def _verify_market_equivalence(
    pm_question:     str,
    kalshi_question: str,
    api_key:         str,
    model:           str = "deepseek-chat",
    timeout:         int = REQUEST_TIMEOUT,
) -> Tuple[str, float]:
    """调用 DeepSeek 判断两个市场是否等价。

    使用精简 Prompt（~50 tokens），节省 API 消耗。

    Returns
    -------
    (verdict, confidence)
    verdict: "yes" | "no" | "uncertain"
    confidence: 0..1
    """
    # 精简 Prompt：截断超长问题，单轮对话，max_tokens=5
    pm_q  = pm_question[:120]
    kal_q = kalshi_question[:120]
    prompt = (
        f'A: "{pm_q}"\n'
        f'B: "{kal_q}"\n'
        "Same event? Reply: yes / no / uncertain"
    )
    payload = json.dumps({
        "model":       model,
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens":  5,
    }).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data    = payload,
        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method  = "POST",
    )

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"].strip().lower()
            if "yes" in text:
                return "yes", 0.95
            if "uncertain" in text:
                return "uncertain", 0.50
            return "no", 0.05
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("DeepSeek 等价验证失败 (attempt %d): %s", attempt, exc)
            if attempt < RETRY_ATTEMPTS:
                time.sleep(1)

    # API 全部失败 → uncertain
    return "uncertain", 0.40


# ---------------------------------------------------------------------------
# 套利计算
# ---------------------------------------------------------------------------

def _best_arb(
    pm: Market,
    kal: KalshiMarket,
    min_gap: float,
) -> Optional[ArbitrageOpportunity]:
    """计算 PM × Kalshi 最优套利方向和利润空间。

    检查两个方向：
      方向 1: Buy PM YES  + Buy Kalshi NO  → gap = 1 - pm.price - kal.no_price
      方向 2: Buy PM NO   + Buy Kalshi YES → gap = 1 - (1-pm.price) - kal.yes_price
    取 gap 最大的方向。若两个方向的 gap 均 < min_gap，返回 None。
    """
    # 方向 1
    gap1 = 1.0 - pm.price - kal.no_price
    # 方向 2
    gap2 = 1.0 - (1.0 - pm.price) - kal.yes_price

    if gap1 >= gap2 and gap1 >= min_gap:
        pm_side, kalshi_side = "YES", "NO"
        pm_price, kalshi_price, arb_gap = pm.price, kal.no_price, gap1
    elif gap2 > gap1 and gap2 >= min_gap:
        pm_side, kalshi_side = "NO", "YES"
        pm_price, kalshi_price, arb_gap = 1.0 - pm.price, kal.yes_price, gap2
    else:
        return None

    total_cost         = pm_price + kalshi_price
    profit_pct         = arb_gap / total_cost if total_cost > 0 else 0.0
    rationale = [
        f"PM {pm_side} @ {pm_price:.3f}  +  Kalshi {kalshi_side} @ {kalshi_price:.3f}",
        f"套利空间 = 1 - {pm_price:.3f} - {kalshi_price:.3f} = {arb_gap:.4f}",
        f"预期收益率 = {profit_pct:.1%}",
    ]
    return ArbitrageOpportunity(
        pm_market           = pm,
        kalshi_market       = kal,
        pm_side             = pm_side,        # type: ignore[arg-type]
        kalshi_side         = kalshi_side,    # type: ignore[arg-type]
        pm_price            = pm_price,
        kalshi_price        = kalshi_price,
        arb_gap             = round(arb_gap,  4),
        expected_profit_pct = round(profit_pct, 4),
        match_confidence    = 0.0,            # 由调用方填写
        match_method        = "keyword",      # 由调用方更新
        rationale           = rationale,
    )


# ---------------------------------------------------------------------------
# ArbitrageScanner — 主类
# ---------------------------------------------------------------------------

class ArbitrageScanner:
    """扫描 Polymarket × Kalshi 跨平台套利机会。

    Parameters
    ----------
    cfg :
        策略配置（套利阈值、AI 验证数量上限等）。
    deepseek_api_key :
        DeepSeek API Key，用于 Stage 2 市场等价验证。
        若不提供且未设置 DEEPSEEK_API_KEY 环境变量，Stage 2 将跳过。
    kalshi_api_key :
        Kalshi API Key（可选，公开端点无需）。
    timeout :
        HTTP 请求超时秒数（默认 15）。
    model :
        DeepSeek 模型名（默认 deepseek-chat）。
    """

    def __init__(
        self,
        cfg:              AccountConfig = DEFAULT_CONFIG,
        deepseek_api_key: Optional[str] = None,
        kalshi_api_key:   Optional[str] = None,
        timeout:          int = REQUEST_TIMEOUT,
        model:            str = "deepseek-chat",
    ):
        self.cfg             = cfg
        self.deepseek_key    = deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self.timeout         = timeout
        self.model           = model
        self._kalshi_fetcher = KalshiFetcher(api_key=kalshi_api_key, timeout=timeout)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def scan(self, pm_markets: List[Market]) -> ArbitrageReport:
        """执行跨平台套利扫描，返回 ArbitrageReport。

        Parameters
        ----------
        pm_markets :
            本轮从 Gamma API 拉取的全量 Polymarket 市场列表。
        """
        report = ArbitrageReport(pm_markets_checked=len(pm_markets))

        # ── Step 1: 拉取 Kalshi 数据 ───────────────────────────────────
        logger.info("ArbitrageScanner: 正在拉取 Kalshi 市场数据…")
        kalshi_markets = self._kalshi_fetcher.fetch_markets(max_markets=500)
        report.kalshi_markets_fetched = len(kalshi_markets)

        if not kalshi_markets:
            logger.warning("Kalshi 数据为空，跳过套利扫描。")
            return report

        # ── Step 2: Jaccard 初筛 ────────────────────────────────────────
        logger.info(
            "ArbitrageScanner: 开始 Jaccard 初筛（PM %d × Kalshi %d）…",
            len(pm_markets), len(kalshi_markets),
        )
        candidates: List[Tuple[Market, KalshiMarket, float]] = []  # (pm, kal, jaccard)

        for pm in pm_markets:
            for kal in kalshi_markets:
                # 快速过滤：Kalshi open_interest 不足，不值得套利
                if kal.open_interest < self.cfg.arb_kalshi_min_oi:
                    continue
                score = _jaccard(pm.question, kal.question)
                if score >= self.cfg.arb_jaccard_threshold:
                    candidates.append((pm, kal, score))

        # 按 Jaccard 分数降序排列
        candidates.sort(key=lambda t: t[2], reverse=True)
        report.candidate_pairs = len(candidates)
        logger.info(
            "ArbitrageScanner: Jaccard 初筛找到 %d 个候选对（阈值 %.2f）",
            len(candidates), self.cfg.arb_jaccard_threshold,
        )

        if not candidates:
            logger.info("ArbitrageScanner: 无候选对，套利扫描结束。")
            return report

        # ── Step 3: AI 等价验证（Top-N 候选）──────────────────────────
        top_candidates = candidates[: self.cfg.arb_ai_verify_top_n]
        verify_with_ai = bool(self.deepseek_key)

        if not verify_with_ai:
            logger.info(
                "ArbitrageScanner: DEEPSEEK_API_KEY 未配置，跳过 Stage 2 AI 验证，"
                "直接使用 Jaccard 置信度。"
            )

        verified: List[Tuple[Market, KalshiMarket, float, str]] = []
        # (pm, kal, confidence, method)

        for pm, kal, jaccard_score in top_candidates:
            if verify_with_ai:
                verdict, confidence = _verify_market_equivalence(
                    pm.question, kal.question,
                    api_key = self.deepseek_key,
                    model   = self.model,
                    timeout = self.timeout,
                )
                report.ai_verified_pairs += 1
                if verdict == "no":
                    logger.debug(
                        "AI: PM[%s] × Kalshi[%s] → 不等价，跳过",
                        pm.market_id[:12], kal.ticker,
                    )
                    continue
                method = "ai_verified" if verdict == "yes" else "ai_uncertain"
                logger.debug(
                    "AI: PM[%s] × Kalshi[%s] → %s (confidence=%.2f)",
                    pm.market_id[:12], kal.ticker, verdict, confidence,
                )
            else:
                # 无 AI：用 Jaccard 分数映射到置信度
                confidence = min(0.85, jaccard_score * 1.2)
                method     = "keyword"

            verified.append((pm, kal, confidence, method))

        logger.info(
            "ArbitrageScanner: %d 个候选对通过验证（AI验证: %d）",
            len(verified), report.ai_verified_pairs,
        )

        # ── Step 4: 计算套利空间，过滤 arb_gap < min_gap ───────────────
        for pm, kal, confidence, method in verified:
            opp = _best_arb(pm, kal, min_gap=self.cfg.arb_min_gap)
            if opp is None:
                continue
            # 填写匹配信息
            opp.match_confidence = round(confidence, 3)
            opp.match_method     = method       # type: ignore[assignment]
            opp.rationale.append(
                f"匹配方式: {method}  置信度: {confidence:.0%}"
            )
            report.opportunities.append(opp)

        # 按套利空间降序排列
        report.opportunities.sort(key=lambda o: o.arb_gap, reverse=True)

        logger.info(
            "ArbitrageScanner: 发现 %d 个套利机会（最大空间: %.4f）",
            len(report.opportunities),
            report.opportunities[0].arb_gap if report.opportunities else 0,
        )
        return report
