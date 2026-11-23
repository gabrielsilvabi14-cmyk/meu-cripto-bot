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
    entry_market_cap: float | None = None
    stop_pct: float = 12.0
    target_pct: float = 50.0
    last_mark_price: float | None = None
    last_mark_at: str | None = None
    entry_score: float | None = None
    migration_status: str = "nao_verificada"
    migration_age_minutes: float | None = None
    peak_price: float | None = None
    dynamic_stop_price: float | None = None
    previous_mark_price: float | None = None
    exit_strategy: str = "adaptive_v3_dynamic"
    last_market_signals: dict = field(default_factory=dict)
    price_samples: list = field(default_factory=list)
    observed_patterns: list = field(default_factory=list)
    entry_patterns: list = field(default_factory=list)
    candle_patterns: list = field(default_factory=list)
    trailing_breaches: int = 0
    last_liquidity: float | None = None

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
        event = {"action": "PAPER_BUY", "token": token, "network": network, "notional_usd": round(notional, 2), "fee_usd": round(fee, 4), "entry_price": execution_price, "timestamp": datetime.now(timezone.utc).isoformat()}
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
        pnl = proceeds - cost_basis
        event = {"action": "PAPER_SELL", "entry_score": pos.entry_score, "migration_status": pos.migration_status, "migration_age_minutes": pos.migration_age_minutes, "stop_config_pct": pos.stop_pct, "target_config_pct": pos.target_pct, "entry_price": pos.entry_price, "observed_price": price, "observed_at": pos.last_mark_at, "stop_trigger_price": pos.entry_price * (1 - pos.stop_pct / 100), "target_trigger_price": pos.entry_price * (1 + pos.target_pct / 100), "stop_overshoot_pct_points": round(max(0.0, (1 - price / pos.entry_price) * 100 - pos.stop_pct), 3) if getattr(pos, "_exit_reason", "") == "stop_loss" else 0.0, "exit_reason": getattr(pos, "_exit_reason", "manual"), "exit_strategy": pos.exit_strategy, "peak_price": pos.peak_price, "dynamic_stop_price": pos.dynamic_stop_price, "opened_at": pos.opened_at, "market_signals": pos.last_market_signals, "observed_patterns": list(pos.observed_patterns), "entry_patterns": list(pos.entry_patterns), "candle_patterns": list(pos.candle_patterns), "price_sample_count": len(pos.price_samples), "token": token, "network": pos.network, "fraction": fraction, "net_usd": round(proceeds, 4), "cost_basis_usd": round(cost_basis, 4), "realized_pnl_usd": round(pnl, 4), "realized_pnl_pct": round(pnl / cost_basis * 100, 2) if cost_basis else 0, "exit_price": price * (1 - self.slippage_pct / 100), "timestamp": datetime.now(timezone.utc).isoformat()}
        self.events.append(event)
        return event

    def evaluate_exit(self, token: str, price: float, market: dict | None = None) -> dict | None:
        """Adaptive PAPER-only exit: protective floor plus profit-protecting trailing stop.

        No fixed take-profit ceiling. A missing price never triggers a sale.
        The protective stop never moves down, even after a drawdown.
        """
        pos = self.positions[token]
        self._valid(price)
        market = market or {}
        prior_liquidity = pos.last_liquidity
        pos.last_market_signals = {k: market[k] for k in ("source", "liquidity", "buy_volume_1m", "sell_volume_1m", "top10_ratio", "bundler_ratio", "insider_ratio") if market.get(k) is not None}
        for pattern in market.get("candle_patterns", []) or []:
            if pattern not in pos.candle_patterns:
                pos.candle_patterns.append(pattern)
        previous = pos.last_mark_price
        pos.previous_mark_price = previous
        pos.last_mark_price = price
        pos.last_mark_at = datetime.now(timezone.utc).isoformat()
        # Samples are observed spot marks, NOT verified OHLCV candles.
        samples = pos.price_samples
        samples.append({"price": price, "buy_volume_1m": market.get("buy_volume_1m"),
                        "sell_volume_1m": market.get("sell_volume_1m")})
        if len(samples) > 120:
            del samples[:-120]
        if len(samples) >= 5:
            recent = [x["price"] for x in samples[-5:]]
            changes = [recent[i] / recent[i-1] - 1 for i in range(1, len(recent))]
            patterns = []
            if all(x > 0 for x in changes) and max(changes) < 0.08:
                patterns.append("alta_gradual_5_amostras")
            if recent[-1] > max(recent[:-1]) * 1.12:
                patterns.append("rompimento_12pct_5_amostras")
            if recent[-1] < max(recent[:-1]) * 0.80:
                patterns.append("queda_20pct_do_pico_5_amostras")
            if min(recent[:-1]) < recent[0] * 0.85 and recent[-1] > recent[-2] * 1.05:
                patterns.append("reversao_apos_queda_5_amostras")
            for pattern in patterns:
                if pattern not in pos.observed_patterns:
                    pos.observed_patterns.append(pattern)
        pos.peak_price = max(pos.peak_price or pos.entry_price, price)
        gain_pct = (pos.peak_price / pos.entry_price - 1) * 100
        score = pos.entry_score if pos.entry_score is not None else 60
        # Volatility-sensitive risk floor, not a fixed -15% stop or +50% target.
        # A gap can still execute well below the floor on the next available mark.
        recent_prices = [x["price"] for x in samples[-7:]]
        moves = [abs(recent_prices[i] / recent_prices[i-1] - 1)
                 for i in range(1, len(recent_prices)) if recent_prices[i-1] > 0]
        typical_move = sorted(moves)[len(moves)//2] if moves else 0.035
        risk_band = min(0.14, max(0.045, typical_move * 2.5))
        floor = pos.entry_price * (1 - risk_band)
        recent_up = len(recent_prices) >= 3 and recent_prices[-1] >= recent_prices[-3]
        buy_vol = market.get("buy_volume_1m")
        sell_vol = market.get("sell_volume_1m")
        pressure_known = isinstance(buy_vol, (int, float)) and isinstance(sell_vol, (int, float))
        healthy = recent_up and pressure_known and buy_vol >= sell_vol
        weakening = pressure_known and sell_vol > max(buy_vol, 1) * 1.4
        pos.exit_strategy = "adaptive_v4_trend"
        if pos.peak_price > pos.entry_price * 1.06:
            trail_band = min(0.22, max(0.10, typical_move * 3.0)) if healthy else min(0.18, max(0.08, typical_move * 2.5))
            floor = max(floor, pos.peak_price * (1 - trail_band))
        if pos.peak_price > pos.entry_price * 1.12:
            floor = max(floor, pos.entry_price * 1.015)
        pos.dynamic_stop_price = max(pos.dynamic_stop_price or 0, floor)
        buy_vol = market.get("buy_volume_1m")
        sell_vol = market.get("sell_volume_1m")
        liquidity = market.get("liquidity")
        severe_selling = (isinstance(buy_vol, (int, float)) and isinstance(sell_vol, (int, float)) and sell_vol >= 3000 and sell_vol > max(buy_vol, 1) * 3)
        liquidity_crisis = isinstance(liquidity, (int, float)) and (liquidity < 5000 or (prior_liquidity is not None and liquidity < prior_liquidity * 0.65))
        if isinstance(liquidity, (int, float)) and isfinite(liquidity) and liquidity > 0:
            pos.last_liquidity = liquidity
        if liquidity_crisis or severe_selling:
            pos._exit_reason = "adaptive_liquidity_risk" if liquidity_crisis else "adaptive_sell_pressure"
            return self.sell(token, price)
        # Realize part of a strong gain only if market risk is not already critical.
        if (not pos.partial_taken and gain_pct >= 10
                and (severe_selling or (previous is not None and price < previous * 0.97)
                     or (gain_pct >= 18 and price < pos.peak_price * 0.94))):
            pos.partial_taken = True
            pos.dynamic_stop_price = max(pos.dynamic_stop_price or 0, pos.entry_price * 1.01)
            pos._exit_reason = "adaptive_partial_profit"
            fraction = 0.5 if weakening else (0.2 if healthy else 0.35)
            return self.sell(token, price, fraction=fraction)
        if price <= pos.dynamic_stop_price:
            pos.trailing_breaches += 1
            # Never delay initial risk stop or a sharp drawdown.
            if gain_pct < 6 or weakening or price < pos.dynamic_stop_price * 0.95 or pos.trailing_breaches >= 2 or price <= pos.entry_price:
                pos._exit_reason = "adaptive_trailing_stop" if gain_pct >= 6 else "adaptive_risk_stop"
                return self.sell(token, price)
        else:
            pos.trailing_breaches = 0
        # Strong negative momentum after a significant gain: protect the run.
        if (previous is not None and gain_pct >= 20
                and price < previous * 0.90 and price < pos.peak_price * 0.90):
            pos._exit_reason = "adaptive_momentum_exit"
            return self.sell(token, price)
        return None
