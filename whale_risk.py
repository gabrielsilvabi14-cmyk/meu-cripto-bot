"""Indicadores de saída e distribuição de holders com dados explícitos da GMGN."""
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DB = os.getenv("RADAR_WHALE_DB", "radar_whales.sqlite3")
BRASILIA = ZoneInfo("America/Sao_Paulo")


def number(v):
    try:
        if isinstance(v, str):
            v = v.strip().replace(",", "")
            if v.endswith("%"):
                v = v[:-1]
        x = float(v)
        return x if math.isfinite(x) and x >= 0 else None
    except (TypeError, ValueError):
        return None


def rows(data):
    """Extrai listas de holders das estruturas JSON usadas pela GMGN."""
    if isinstance(data, list):
        return [v for v in data if isinstance(v, dict)]
    if isinstance(data, dict):
        for key in ("holders", "list", "items", "data", "top_holders",
                    "result", "rank", "token_holders"):
            if key in data:
                result = rows(data[key])
                if result:
                    return result
    return []


def holder_address(item):
    for key in ("wallet_address", "holder_address", "owner", "wallet",
                "address", "account"):
        value = item.get(key)
        if isinstance(value, str) and len(value) >= 20:
            return value
    return None


def holder_value(item):
    """Valor em USD apenas quando a resposta declara essa unidade."""
    for key in ("value_usd", "usd_value", "holding_value_usd", "balance_usd",
                "amount_usd", "total_value_usd"):
        v = number(item.get(key))
        if v is not None:
            return v
    return None


def holder_tokens(item):
    """Quantidade de tokens em campos explícitos; raw balances são omitidos."""
    for key in ("token_amount", "amount", "balance", "token_balance",
                "balance_tokens", "holding_amount", "quantity", "amount_cur"):
        v = number(item.get(key))
        if v is not None:
            return v
    return None


def holder_percent(item):
    for key in ("percentage", "percent", "holding_percentage",
                "token_percentage", "amount_percentage", "balance_percentage",
                "holding_rate", "amount_rate", "supply_rate", "share"):
        raw = number(item.get(key))
        if raw is None:
            continue
        if key.endswith("_rate"):
            if raw <= 1:
                return raw * 100
            continue
        if raw <= 100:
            return raw
    return None


def _parse_date(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp <= 0:
            return None
        if stamp > 100_000_000_000:
            stamp /= 1000
        try:
            return datetime.fromtimestamp(stamp, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    for pattern in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def wallet_created(item):
    # Só datas explicitamente associadas à criação da carteira.
    for key in ("wallet_created_at", "wallet_creation_time",
                "wallet_create_time", "create_timestamp",
                "creation_timestamp", "creation_time", "created_timestamp",
                "created_at", "create_time", "create_at"):
        parsed = _parse_date(item.get(key))
        if parsed:
            return parsed
    return None


def _db():
    con = sqlite3.connect(DB, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS whale_snapshots
        (chain TEXT, mint TEXT, wallet TEXT, observed INTEGER, value_usd REAL,
         PRIMARY KEY(chain,mint,wallet,observed))""")
    return con


def _short(address):
    return address if len(address) <= 14 else f"{address[:7]}…{address[-5:]}"


def _usd(value):
    return f"US$ {value:,.2f}"


def analyze(pair, holders_data=None):
    liquidity = number((pair.get("liquidity") or {}).get("usd"))
    volume = number((pair.get("volume") or {}).get("h1"))
    cap = number(pair.get("marketCap"))
    lines = ["<b>🐋 LIQUIDEZ E BALEIAS</b>"]
    if liquidity and volume is not None:
        multiple = volume / liquidity
        lines.append(f"📊 Volume 1h / liquidez: {multiple:.1f}x (não mede profundidade)")
        if multiple > 10:
            lines.append("🟠 Giro muito elevado em relação à liquidez")
    if liquidity and cap is not None:
        lines.append(f"📉 Market cap / liquidez: {cap/liquidity:.1f}x (indicador indireto)")

    parsed = []
    for item in rows(holders_data):
        address = holder_address(item)
        if not address:
            continue
        parsed.append({
            "address": address,
            "usd": holder_value(item),
            "tokens": holder_tokens(item),
            "percent": holder_percent(item),
            "created": wallet_created(item),
        })

    if parsed:
        by_day = {}
        for holder in parsed:
            if holder["created"] is not None:
                local_day = holder["created"].astimezone(BRASILIA).date().isoformat()
                by_day.setdefault(local_day, []).append(holder["address"])
        same_day = [(day, wallets) for day, wallets in by_day.items() if len(wallets) > 1]
        if same_day:
            day, wallets = max(same_day, key=lambda entry: len(entry[1]))
            shown_day = datetime.fromisoformat(day).strftime("%d/%m")
            lines.append(
                f"👥 Carteiras: {len(parsed)} analisadas; {len(wallets)} criadas em "
                f"{shown_day} (data igual não comprova vínculo)"
            )
        elif any(holder["created"] is None for holder in parsed):
            lines.append(
                f"👥 Carteiras: {len(parsed)} analisadas; datas incompletas"
            )
        else:
            lines.append(f"👥 Carteiras analisadas: {len(parsed)}")
 
    usd_positions = [
        (holder["address"], holder["usd"])
        for holder in parsed if holder["usd"] is not None
    ]
    if usd_positions and liquidity:
        largest = max(usd_positions, key=lambda v: v[1])
        lines.append(
            f"🐳 Maior posição informada / liquidez: "
            f"{largest[1]/liquidity:.1f}x (não equivale ao impacto de venda)"
        )
        chain = pair.get("chainId") or ""
        mint = (pair.get("baseToken") or {}).get("address") or ""
        if mint:
            try:
                now = int(time.time())
                with _db() as con:
                    previous = con.execute(
                        "SELECT value_usd FROM whale_snapshots "
                        "WHERE chain=? AND mint=? AND wallet=? AND observed<? "
                        "ORDER BY observed DESC LIMIT 1",
                        (chain, mint, largest[0], now-60)).fetchone()
                    for wallet, value in usd_positions[:30]:
                        con.execute(
                            "INSERT OR REPLACE INTO whale_snapshots VALUES (?,?,?,?,?)",
                            (chain, mint, wallet, now, value))
                    con.execute("DELETE FROM whale_snapshots WHERE observed<?", (now-7*86400,))
                if previous and previous[0] > 0:
                    delta = (largest[1]/previous[0]-1)*100
                    lines.append(
                        f"📍 Variação da maior carteira observada: {delta:+.1f}% "
                        "(inclui efeito do preço)"
                    )
            except (sqlite3.Error, OSError):
                pass
    elif not parsed:
        lines.append("⚪ Lista de holders indisponível nesta consulta GMGN.")

    return "\n".join(lines) if len(lines) > 1 else ""


def evaluate(pair, holders=None):
    return analyze(pair, holders)
