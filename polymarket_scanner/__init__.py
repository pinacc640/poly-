"""Polymarket market scanner — Phase 1 + Phase 2.

Phase 1: MarketScanner — stable convergence + volatility sleeve strategies
Phase 2: PositionMonitor — hold/add/exit signals on existing positions
         ArbitrageScanner — cross-exchange arbitrage vs Kalshi
"""

from .scanner   import MarketScanner
from .models    import (
    Market,
    Position,
    StableOpportunity,
    VolatilityOpportunity,
    RiskDecision,
    # Phase 2
    PositionSignal,
    MonitorReport,
    KalshiMarket,
    ArbitrageOpportunity,
    ArbitrageReport,
)
from .ai_oracle             import AIOracle
from .position_monitor      import PositionMonitor
from .arbitrage_scanner     import ArbitrageScanner, KalshiFetcher

__all__ = [
    # Phase 1
    "MarketScanner",
    "Market",
    "Position",
    "StableOpportunity",
    "VolatilityOpportunity",
    "RiskDecision",
    "AIOracle",
    # Phase 2
    "PositionSignal",
    "MonitorReport",
    "KalshiMarket",
    "ArbitrageOpportunity",
    "ArbitrageReport",
    "PositionMonitor",
    "ArbitrageScanner",
    "KalshiFetcher",
]
