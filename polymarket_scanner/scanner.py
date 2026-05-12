"""MarketScanner — top-level orchestrator.

Usage:
    scanner = MarketScanner()              # uses DEFAULT_CONFIG + mock data
    report  = scanner.run()               # returns ScanReport

To plug in a real data source later, subclass MarketScanner and override
`fetch_markets()`. Everything else (strategies, risk gate, formatting)
stays untouched.
"""

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .config import AccountConfig, DEFAULT_CONFIG
from .mock_data import load_mock_markets
from .models import Market, RiskDecision, StableOpportunity, VolatilityOpportunity
from .risk import RiskController
from .strategies import smart_money_strategy, stable_strategy, volatility_strategy


# ---------------------------------------------------------------------------
# Report container
# ---------------------------------------------------------------------------
@dataclass
class ScanReport:
    # Approved opportunities (passed risk controller)
    stable_approved:      List[tuple] = field(default_factory=list)
    volatility_approved:  List[tuple] = field(default_factory=list)
    smart_money_approved: List[tuple] = field(default_factory=list)

    # Rejected for transparency / debugging
    stable_rejected:      List[tuple] = field(default_factory=list)
    volatility_rejected:  List[tuple] = field(default_factory=list)
    smart_money_rejected: List[tuple] = field(default_factory=list)

    # Metadata
    total_markets_scanned: int = 0
    config: Optional[AccountConfig] = None
    ai_oracle_used: bool = False


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------
class MarketScanner:
    """Orchestrates data fetch → strategies → risk gate → report.

    Parameters
    ----------
    cfg :
        Account / strategy configuration.  Defaults to DEFAULT_CONFIG.
    data_source :
        Zero-argument callable that returns a list of Market objects.
        Defaults to the mock dataset.
    use_ai :
        Placeholder flag. AI enrichment is handled upstream in live_scanner.py.
    """

    def __init__(
        self,
        cfg: AccountConfig = DEFAULT_CONFIG,
        data_source: Callable[[], List[Market]] = load_mock_markets,
        use_ai: bool = False,
    ):
        self.cfg         = cfg
        self.data_source = data_source
        self.use_ai      = use_ai

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
