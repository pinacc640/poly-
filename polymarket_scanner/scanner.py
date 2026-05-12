"""MarketScanner — top-level orchestrator.

Phase 3 changes
---------------
- ScanReport grows smart_money_approved / smart_money_rejected lists
- ScanReport.ai_oracle_used flag (set by live_scanner.py)
- MarketScanner.run() now also runs smart_money_strategy
"""

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .config import AccountConfig, DEFAULT_CONFIG
from .mock_data import load_mock_markets
from .models import Market, RiskDecision, SmartMoneyOpportunity
from .risk import RiskController
from .strategies import smart_money_strategy, stable_strategy, volatility_strategy


# ---------------------------------------------------------------------------
# Report container
# ---------------------------------------------------------------------------
@dataclass
class ScanReport:
    # Approved
    stable_approved:      List[tuple] = field(default_factory=list)
    volatility_approved:  List[tuple] = field(default_factory=list)
    smart_money_approved: List[tuple] = field(default_factory=list)

    # Rejected (audit trail)
    stable_rejected:      List[tuple] = field(default_factory=list)
    volatility_rejected:  List[tuple] = field(default_factory=list)
    smart_money_rejected: List[tuple] = field(default_factory=list)

    # Metadata
    total_markets_scanned: int = 0
    config: Optional[AccountConfig] = None
    ai_oracle_used: bool = False          # set externally by live_scanner.py


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------
class MarketScanner:
    def __init__(
        self,
        cfg: AccountConfig = DEFAULT_CONFIG,
        data_source: Callable[[], List[Market]] = load_mock_markets,
    ):
        self.cfg         = cfg
        self.data_source = data_source

    def fetch_markets(self) -> List[Market]:
        return self.data_source()

    def run(self) -> ScanReport:
        markets = self.fetch_markets()
        report  = ScanReport(
            total_markets_scanned = len(markets),
            config                = self.cfg,
        )

        rc = RiskController(self.cfg)

        # --- Stable sleeve ---
        for opp in stable_strategy(markets, self.cfg):
            dec = rc.approve(opp)
            (report.stable_approved if dec.approved else report.stable_rejected).append(
                (opp, dec)
            )

        # --- Volatility sleeve ---
        for opp in volatility_strategy(markets, self.cfg):
            dec = rc.approve(opp)
            (report.volatility_approved if dec.approved else report.volatility_rejected).append(
                (opp, dec)
            )

        # --- Smart Money 2.0 sleeve ---
        for opp in smart_money_strategy(markets, self.cfg):
            dec = rc.approve(opp)
            (report.smart_money_approved if dec.approved else report.smart_money_rejected).append(
                (opp, dec)
            )

        return report
