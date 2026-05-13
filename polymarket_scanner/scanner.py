"""MarketScanner — top-level orchestrator.

Usage:
    scanner = MarketScanner()              # uses DEFAULT_CONFIG + mock data
    report  = scanner.run()               # returns ScanReport

To plug in a real data source later, subclass MarketScanner and override
`fetch_markets()`. Everything else (strategies, risk gate, formatting)
stays untouched.

持仓过滤
--------
如果传入 held_market_ids（已持仓的 market_id 集合），扫描器会在策略评估前
自动过滤掉这些市场，防止重复推送已买入的标的。
"""

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set

from .config import AccountConfig, DEFAULT_CONFIG
from .mock_data import load_mock_markets
from .models import Market, RiskDecision, StableOpportunity, VolatilityOpportunity
from .risk import RiskController
from .strategies import stable_strategy, volatility_strategy


# ---------------------------------------------------------------------------
# Report container
# ---------------------------------------------------------------------------
@dataclass
class ScanReport:
    # Approved opportunities (passed risk controller)
    stable_approved:     List[tuple]  = field(default_factory=list)   # (StableOpportunity, RiskDecision)
    volatility_approved: List[tuple]  = field(default_factory=list)   # (VolatilityOpportunity, RiskDecision)

    # Rejected for transparency / debugging
    stable_rejected:     List[tuple]  = field(default_factory=list)
    volatility_rejected: List[tuple]  = field(default_factory=list)

    # Metadata
    total_markets_scanned: int = 0
    already_held_skipped:  int = 0   # 因已持仓而跳过的市场数
    config: Optional[AccountConfig] = None


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------
class MarketScanner:
    """Orchestrates data fetch → position filter → strategies → risk gate → report.

    Parameters
    ----------
    cfg :
        Account / strategy configuration.  Defaults to DEFAULT_CONFIG.
    data_source :
        Zero-argument callable that returns a list of Market objects.
        Defaults to the mock dataset.  Swap this for a real API client
        without changing any other code.
    held_market_ids :
        已持仓市场的 market_id 集合（由 PositionFetcher.held_market_ids() 提供）。
        集合中的市场将在策略扫描前被过滤掉，避免重复推送。
        默认为空集合（不过滤）。
    """

    def __init__(
        self,
        cfg: AccountConfig = DEFAULT_CONFIG,
        data_source: Callable[[], List[Market]] = load_mock_markets,
        held_market_ids: Optional[Set[str]] = None,
    ):
        self.cfg             = cfg
        self.data_source     = data_source
        self.held_market_ids: Set[str] = held_market_ids or set()

    # ------------------------------------------------------------------
    # Override this method to connect a real Polymarket API client
    # ------------------------------------------------------------------
    def fetch_markets(self) -> List[Market]:
        """Return raw market records from the configured data source."""
        return self.data_source()

    # ------------------------------------------------------------------
    # 持仓过滤（核心去重逻辑）
    # ------------------------------------------------------------------
    def _filter_held(self, markets: List[Market]) -> tuple[List[Market], int]:
        """从候选列表中移除已持仓市场，返回 (过滤后列表, 跳过数量)。"""
        if not self.held_market_ids:
            return markets, 0
        filtered = [m for m in markets if m.market_id not in self.held_market_ids]
        skipped  = len(markets) - len(filtered)
        if skipped:
            import logging
            logging.getLogger(__name__).info(
                "🚫 已过滤 %d 个已持仓市场，避免重复推送。", skipped
            )
        return filtered, skipped

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run(self) -> ScanReport:
        all_markets = self.fetch_markets()

        # ── 持仓去重过滤 ──────────────────────────────────────────────
        markets, skipped = self._filter_held(all_markets)

        report = ScanReport(
            total_markets_scanned = len(all_markets),
            already_held_skipped  = skipped,
            config                = self.cfg,
        )

        rc = RiskController(self.cfg)   # shared controller tracks vol sleeve usage

        # --- Stable sleeve ---
        stable_candidates = stable_strategy(markets, self.cfg)
        for opp in stable_candidates:
            decision = rc.approve(opp)
            if decision.approved:
                report.stable_approved.append((opp, decision))
            else:
                report.stable_rejected.append((opp, decision))

        # --- Volatility sleeve (shared risk controller preserves sleeve cap) ---
        vol_candidates = volatility_strategy(markets, self.cfg)
        for opp in vol_candidates:
            decision = rc.approve(opp)
            if decision.approved:
                report.volatility_approved.append((opp, decision))
            else:
                report.volatility_rejected.append((opp, decision))

        return report
