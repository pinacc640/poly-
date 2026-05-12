"""Polymarket market scanner MVP.

A modular, extensible scanner for small-capital Polymarket accounts that
splits attention between high-conviction stable convergence trades and
a small sleeve of short-horizon volatility trades, gated by a strict
risk controller.
"""

from .scanner import MarketScanner
from .models import Market, StableOpportunity, VolatilityOpportunity, RiskDecision
from .ai_oracle import AIOracle

__all__ = [
    "MarketScanner",
    "Market",
    "StableOpportunity",
    "VolatilityOpportunity",
    "RiskDecision",
    "AIOracle",
]
