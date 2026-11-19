"""Read-only GMGN adapter skeleton. No undocumented endpoint assumptions."""
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class TokenSnapshot:
    chain: str
    contract: str
    price_usd: float | None
    liquidity_usd: float | None
    volume_1h_usd: float | None
    buys_1h: int | None
    sells_1h: int | None
    source: str | None
    security: dict[str, Any]

def normalize_token(payload: dict[str, Any], chain: str) -> TokenSnapshot:
    """Normalize ONLY explicit data; absent fields stay unknown (None)."""
    if chain not in ("sol", "eth"):
        raise ValueError("Unsupported network")
    def num(name):
        value = payload.get(name)
        try:
            return float(value) if value is not None and value != "" else None
        except (ValueError, TypeError):
            return None
    def integer(name):
        value = payload.get(name)
        try:
            return int(value) if value is not None and value != "" else None
        except (ValueError, TypeError):
            return None
    return TokenSnapshot(chain=chain, contract=str(payload.get("address") or ""), price_usd=num("price"), liquidity_usd=num("liquidity"), volume_1h_usd=num("volume_1h"), buys_1h=integer("buys_1h"), sells_1h=integer("sells_1h"), source=payload.get("source"), security=payload.get("security") if isinstance(payload.get("security"), dict) else {})
