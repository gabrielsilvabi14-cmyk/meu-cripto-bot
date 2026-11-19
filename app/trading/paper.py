"""Deterministic in-memory paper engine. No wallet signing, no network orders."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from .risk import RiskConfig, position_size

@dataclass
class Position:
    token: str
    network: str
    quantity: float
    entry_price: float
    entry_cost_usd: float
    opened_at: str
    partial_taken: bool = False

@dataclass
class PaperAccount:
    cash_usd: float = 100.0
    positions: dict[str, Position] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    config: RiskConfig = field(default_factory=RiskConfig)
    fee_pct: float = 1.0
    slippage_pct: float = 1.0

    def _valid(self, price: float):
        if not isfinite(price) or price <= 0:
            raise ValueError("Invalid market price")

    def buy(self, token: str, network: str, price: float, liquidity_usd: float) -> dict:
        self._valid(price)
        if network not in ("sol", "eth") or not token:
            raise ValueError("Invalid token/network")
        if not isfinite(liquidity_usd) or liquidity_usd < self.config.min_liquidity_usd:
            raise ValueError("Insufficient reported liquidity")
        if token in self.positions or len(self.positions) >= self.config.max_open_positions:
            raise ValueError("Position limit")
        notional = position_size(self.cash_usd, self.config, self.fee_pct * 2 + self.slippage_pct * 2)
        if notional <= 0:
            raise ValueError("No capital")
        execution_price = price * (1 + self.slippage_pct / 100)
        fee = notional * self.fee_pct / 100
        total = notional + fee
        if total > self.cash_usd:
            raise ValueError("Insufficient paper cash")
        self.cash_usd -= total
        self.positions[token] = Position(token, network, notional / execution_price, execution_price, total, datetime.now(timezone.utc).isoformat())
        event = {"action": "PAPER_BUY", "token": token, "network": network, "notional_usd": round(notional, 2), "fee_usd": round(fee, 4)}
        self.events.append(event)
        return event

    def sell(self, token: str, price: float, fraction: float = 1.0) -> dict:
        self._valid(price)
        if not 0 < fraction <= 1 or not isfinite(fraction):
            raise ValueError("Invalid fraction")
        pos = self.positions[token]
        qty = pos.quantity * fraction
        gross = qty * price * (1 - self.slippage_pct / 100)
        fee = gross * self.fee_pct / 100
        proceeds = gross - fee
        cost_basis = pos.entry_cost_usd * fraction
        self.cash_usd += proceeds
        pos.quantity -= qty
        pos.entry_cost_usd -= cost_basis
        if fraction == 1.0 or pos.quantity < 1e-12:
            del self.positions[token]
        event = {"action": "PAPER_SELL", "token": token, "fraction": fraction, "net_usd": round(proceeds, 4), "realized_pnl_usd": round(proceeds - cost_basis, 4)}
        self.events.append(event)
        return event

    def evaluate_exit(self, token: str, price: float) -> dict | None:
        """Stop -12%; partial sale of 50% at +25%, then remaining at +50%."""
        pos = self.positions[token]
        self._valid(price)
        change_pct = (price / pos.entry_price - 1) * 100
        if change_pct <= -self.config.stop_pct:
            return self.sell(token, price)
        if change_pct >= 50 and pos.partial_taken:
            return self.sell(token, price)
        if change_pct >= 25 and not pos.partial_taken:
            pos.partial_taken = True
            return self.sell(token, price, 0.5)
        return None
