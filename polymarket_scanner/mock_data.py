"""Mock dataset used for local testing.

The 10 records are hand-crafted to exercise every branch of the
stable and volatility strategies plus the risk controller:

- M1  Strong stable YES candidate (politics, near expiry, high price)
- M2  Strong stable NO  candidate (sports, low price, volume rising)
- M3  Stable candidate rejected by macro blocklist (oil)
- M4  Stable candidate rejected by low liquidity
- M5  Stable candidate rejected by political shock (score penalty)
- M6  Volatility candidate: clean +6% move, no fundamental change
- M7  Volatility candidate rejected: fundamental change flag set
- M8  Volatility candidate: negative move, shorts the YES side
- M9  Edge case: passes stable filters but EV <= 0 (true_prob too low)
- M10 Edge case: passes everything but profit below $0.50 floor
"""

from typing import List

from .models import Market


def load_mock_markets() -> List[Market]:
    return [
        Market(
            market_id="M1",
            question="Will Candidate A win the state primary?",
            category="politics",
            price=0.92,
            liquidity=450_000,
            volume_24h=80_000,
            volume_prev_24h=55_000,
            price_change_24h=0.01,
            days_to_expiry=5,
            true_prob=0.96,
        ),
        Market(
            market_id="M2",
            question="Will Team X be relegated this season?",
            category="sports",
            price=0.08,
            liquidity=220_000,
            volume_24h=40_000,
            volume_prev_24h=25_000,
            price_change_24h=-0.02,
            days_to_expiry=10,
            true_prob=0.04,
        ),
        Market(
            market_id="M3",
            question="Will Brent crude close above $95 this week?",
            category="oil",
            price=0.85,
            liquidity=600_000,
            volume_24h=120_000,
            volume_prev_24h=70_000,
            price_change_24h=0.02,
            days_to_expiry=4,
            true_prob=0.90,
        ),
        Market(
            market_id="M4",
            question="Will tiny-cap token flip $1?",
            category="crypto",
            price=0.90,
            liquidity=35_000,                # too shallow
            volume_24h=5_000,
            volume_prev_24h=4_500,
            price_change_24h=0.00,
            days_to_expiry=6,
            true_prob=0.93,
        ),
        Market(
            market_id="M5",
            question="Will Bill Y pass the Senate vote?",
            category="politics",
            price=0.88,
            liquidity=300_000,
            volume_24h=50_000,
            volume_prev_24h=60_000,          # volume declining
            price_change_24h=-0.03,
            days_to_expiry=12,
            true_prob=0.90,
            has_political_shock=True,        # -3 penalty
        ),
        Market(
            market_id="M6",
            question="Will movie Z gross > $200M opening weekend?",
            category="entertainment",
            price=0.68,                      # spiked hard -> fade: buy NO
            liquidity=180_000,
            volume_24h=75_000,
            volume_prev_24h=40_000,
            price_change_24h=0.18,           # large +18% move triggers vol scan
            days_to_expiry=9,
            true_prob=0.04,                  # analyst says 96% chance it FAILS -> NO EV strong
        ),
        Market(
            market_id="M7",
            question="Will startup S close Series B by EOM?",
            category="business",
            price=0.42,
            liquidity=160_000,
            volume_24h=60_000,
            volume_prev_24h=30_000,
            price_change_24h=0.07,
            days_to_expiry=15,
            true_prob=0.45,
            has_fundamental_change=True,     # disqualifies vol arbitrage
        ),
        Market(
            market_id="M8",
            question="Will rate cut happen at next FOMC?",
            category="macro",
            price=0.28,                      # dropped sharply -> fade: buy YES
            liquidity=500_000,
            volume_24h=200_000,
            volume_prev_24h=110_000,
            price_change_24h=-0.20,          # large -20% drop triggers vol scan
            days_to_expiry=20,
            true_prob=0.90,                  # analyst high conviction YES -> EV strong
        ),
        Market(
            market_id="M9",
            question="Will athlete A break world record this month?",
            category="sports",
            price=0.85,
            liquidity=140_000,
            volume_24h=30_000,
            volume_prev_24h=20_000,
            price_change_24h=0.01,
            days_to_expiry=6,
            true_prob=0.70,                  # too low vs 0.85 -> EV < 0
        ),
        Market(
            market_id="M10",
            question="Will minor bill XYZ pass committee?",
            category="politics",
            price=0.90,
            liquidity=120_000,
            volume_24h=15_000,
            volume_prev_24h=10_000,
            price_change_24h=0.00,
            days_to_expiry=7,
            true_prob=0.915,                 # edge so thin profit < $0.50
        ),
    ]
