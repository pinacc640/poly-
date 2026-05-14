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
from .strategies import stable_strategy, volatility_strategy


# ---------------------------------------------------------------------------
# Report container
# ---------------------------------------------------------------------------
@dataclass
class ScanReport:
    # Approved opportunities (passed risk controller)
    stable_approved:     List[tuple]  = field(default_factory=list)   # (StableOpportunity, RiskDecision)
    volatility_approved: List[tuple]  = field(default_factory=list)   # (VolatilityOpportunity, RiskDecision)
    smart_money_approved: List[tuple] = field(default_factory=list)   # (SmartMoneyOpportunity, RiskDecision)
    arbitrage_found:     List[tuple]  = field(default_factory=list)   # (ArbitrageOpportunity, RiskDecision)

    # Rejected for transparency / debugging
    stable_rejected:     List[tuple]  = field(default_factory=list)
    volatility_rejected: List[tuple]  = field(default_factory=list)
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
        Defaults to the mock dataset.  Swap this for a real API client
        without changing any other code.
    """

    def __init__(
        self,
        cfg: AccountConfig = DEFAULT_CONFIG,
        data_source: Callable[[], List[Market]] = load_mock_markets,
    ):
        self.cfg = cfg
        self.data_source = data_source

    # ------------------------------------------------------------------
    # Override this method to connect a real Polymarket API client
    # ------------------------------------------------------------------
    def fetch_markets(self) -> List[Market]:
        """Return raw market records from the configured data source."""
        return self.data_source()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run(self, use_ai: bool = False, run_arbitrage: bool = False) -> ScanReport:
        markets = self.fetch_markets()
        report  = ScanReport(
            total_markets_scanned=len(markets),
            config=self.cfg,
            ai_oracle_used=use_ai,
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

        # --- Smart Money sleeve ---
        try:
            from polymarket_scanner.strategies.smart_money import smart_money_strategy
            smart_candidates = smart_money_strategy(markets, self.cfg)
            for opp in smart_candidates:
                decision = rc.approve(opp)
                if decision.approved:
                    report.smart_money_approved.append((opp, decision))
                else:
                    report.smart_money_rejected.append((opp, decision))
        except ImportError:
            pass

        # --- Arbitrage sleeve ---
        if run_arbitrage:
            try:
                from polymarket_scanner.strategies.arbitrage import arbitrage_strategy
                arb_candidates = arbitrage_strategy(markets, self.cfg)
                for opp in arb_candidates:
                    decision = rc.approve(opp)
                    if decision.approved:
                        report.arbitrage_found.append((opp, decision))
            except ImportError:
                pass

        return report
