"""Conservative position sizing for PAPER trading only."""
from dataclasses import dataclass
from math import isfinite

@dataclass(frozen=True)
class RiskConfig:
    risk_pct: float = 2.0
    stop_pct: float = 12.0
    max_allocation_pct: float = 20.0
    max_open_positions: int = 3
    min_liquidity_usd: float = 10_000.0

def position_size(equity_usd: float, config: RiskConfig = RiskConfig(), estimated_roundtrip_cost_pct: float = 2.0) -> float:
    """Max paper notional based on equity and planned loss including estimated costs.

    A stop is not guaranteed; liquidity gaps and honeypots can lose 100% of notional.
    """
    vals = (equity_usd, config.risk_pct, config.stop_pct, config.max_allocation_pct, estimated_roundtrip_cost_pct)
    if not all(isfinite(v) for v in vals) or equity_usd <= 0:
        return 0.0
    if not (0 < config.risk_pct <= 5 and 0 < config.stop_pct < 100 and 0 < config.max_allocation_pct <= 100):
        raise ValueError("Invalid risk parameters")
    if estimated_roundtrip_cost_pct < 0:
        raise ValueError("Negative cost estimate")
    risk_budget = equity_usd * config.risk_pct / 100
    by_risk = risk_budget / ((config.stop_pct + estimated_roundtrip_cost_pct) / 100)
    by_allocation = equity_usd * config.max_allocation_pct / 100
    return round(min(by_risk, by_allocation, equity_usd), 2)
