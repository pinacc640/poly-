"""MarketScanner — top-level orchestrator.

Usage:
    scanner = MarketScanner()              # uses DEFAULT_CONFIG + mock data
    report  = scanner.run()               # returns ScanReport

Optional upgrades:
    scanner = MarketScanner(use_ai=True)       # replace true_prob with AI oracle
    scanner = MarketScanner(run_arbitrage=True) # add Kalshi cross-platform scan

To plug in a real data source later, subclass MarketScanner and override
`fetch_markets()`. Everything else (strategies, risk gate, formatting)
stays untouched.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .config import AccountConfig, DEFAULT_CONFIG
from .mock_data import load_mock_markets
from .models import (
    ArbitrageOpportunity,
    Market,
    RiskDecision,
    SmartMoneyOpportunity,
    StableOpportunity,
    VolatilityOpportunity,
)
from .risk import RiskController
from .strategies import (
    arbitrage_strategy,
    smart_money_strategy,
    stable_strategy,
    volatility_strategy,
)


# ---------------------------------------------------------------------------
# Report container
# ---------------------------------------------------------------------------
@dataclass
class ScanReport:
    # Approved opportunities (passed risk controller)
    stable_approved:      List[tuple] = field(default_factory=list)
    volatility_approved:  List[tuple] = field(default_factory=list)
    smart_money_approved: List[tuple] = field(default_factory=list)
    # Arbitrage opportunities are pre-vetted (no risk controller needed —
    # they are by definition risk-free); stored as plain list.
    arbitrage_found:      List[ArbitrageOpportunity] = field(default_factory=list)

    # Rejected for transparency / debugging
    stable_rejected:      List[tuple] = field(default_factory=list)
    volatility_rejected:  List[tuple] = field(default_factory=list)
    smart_money_rejected: List[tuple] = field(default_factory=list)

    # Metadata
    total_markets_scanned: int = 0
    kalshi_markets_scanned: int = 0
    config: Optional[AccountConfig] = None
    ai_oracle_used: bool = False
    run_arbitrage: bool = False   # mirrors MarketScanner.run_arbitrage for formatter


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------
class MarketScanner:
    """Orchestrates data fetch → AI oracle (opt.) → strategies → risk gate → report.

    Parameters
    ----------
    cfg :
        Account / strategy configuration.  Defaults to DEFAULT_CONFIG.
    data_source :
        Zero-argument callable that returns a list of Market objects.
        Defaults to the mock dataset.
    use_ai :
        If True, replace every market's true_prob with an AI-estimated
        value before running strategies.  Requires SILICONFLOW_API_KEY.
    run_arbitrage :
        If True, also fetch Kalshi markets and run the arbitrage scanner.
    kalshi_data_source :
        Zero-argument callable returning a list of raw Kalshi market dicts.
        If None and run_arbitrage=True, KalshiClient is instantiated automatically.
    """

    def __init__(
        self,
        cfg: AccountConfig = DEFAULT_CONFIG,
        data_source: Callable[[], List[Market]] = load_mock_markets,
        use_ai: bool = False,
        run_arbitrage: bool = False,
        kalshi_data_source: Optional[Callable[[], List[Dict]]] = None,
    ):
        self.cfg               = cfg
        self.data_source       = data_source
        self.use_ai            = use_ai
        self.run_arbitrage     = run_arbitrage
        self._kalshi_source    = kalshi_data_source

    # ------------------------------------------------------------------
    def fetch_markets(self) -> List[Market]:
        return self.data_source()

    def fetch_kalshi_markets(self) -> List[Dict]:
        """Return raw Kalshi market dicts (normalised form)."""
        if self._kalshi_source is not None:
            return self._kalshi_source()
        # Auto-instantiate KalshiClient
        from .kalshi_client import KalshiClient, normalise_kalshi_market
        client = KalshiClient()
        raw    = client.fetch_active_markets(limit=self.cfg.arb_kalshi_limit)
        return [n for r in raw if (n := normalise_kalshi_market(r)) is not None]

    # ------------------------------------------------------------------
    def run(self) -> ScanReport:
        markets = self.fetch_markets()
        report  = ScanReport(
            total_markets_scanned=len(markets),
            config=self.cfg,
            run_arbitrage=self.run_arbitrage,
        )

        # ── Optional: AI Oracle probability enrichment ─────────────
        if self.use_ai:
            from .ai_oracle import AiOracle
            oracle = AiOracle(timeout=self.cfg.ai_oracle_timeout)
            if oracle.is_available():
                # Limit API calls: enrich only top-N markets by volume
                top = sorted(markets, key=lambda m: m.volume_24h, reverse=True)
                top = top[: self.cfg.ai_oracle_max_markets]
                oracle.batch_estimate(top)
                report.ai_oracle_used = True
            else:
                import logging
                logging.getLogger(__name__).warning(
                    "[AI Oracle] SILICONFLOW_API_KEY not set — skipping."
                )

        rc = RiskController(self.cfg)

        # ── Stable sleeve ───────────────────────────────────────────
        for opp in stable_strategy(markets, self.cfg):
            dec = rc.approve(opp)
            (report.stable_approved if dec.approved else report.stable_rejected).append(
                (opp, dec)
            )

        # ── Volatility sleeve ───────────────────────────────────────
        for opp in volatility_strategy(markets, self.cfg):
            dec = rc.approve(opp)
            (report.volatility_approved if dec.approved else report.volatility_rejected).append(
                (opp, dec)
            )

        # ── Smart Money sleeve ──────────────────────────────────────
        for opp in smart_money_strategy(markets, self.cfg):
            dec = rc.approve(opp)
            (report.smart_money_approved if dec.approved else report.smart_money_rejected).append(
                (opp, dec)
            )

        # ── Cross-platform arbitrage (optional) ─────────────────────
        if self.run_arbitrage:
            kalshi_markets = self.fetch_kalshi_markets()
            report.kalshi_markets_scanned = len(kalshi_markets)
            report.arbitrage_found = arbitrage_strategy(
                markets, kalshi_markets, self.cfg
            )

        return report
