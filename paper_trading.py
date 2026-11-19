"""Paper wallet using live GMGN prices and an explicitly estimated pool fill.

This module never signs or submits blockchain transactions. It models a constant
product pool from GMGN's reported liquidity; real routing, taxes, MEV and fills
can differ. GMGN's documented 1% transaction fee is included. Network fees are
configurable because the user's live wallet settings/network conditions vary.
"""
import html
import os
import sqlite3
from contextlib import contextmanager
import time
from pathlib import Path

_STATE_PATH = Path(os.getenv("RADAR_STATE_FILE", "radar_state.json"))
DB_PATH = Path(os.getenv("PAPER_DB_PATH", str(_STATE_PATH.with_name("paper_trading.sqlite3"))))
INITIAL_USDT = max(0.0, float(os.getenv("PAPER_INITIAL_USDT", "100")))
GMGN_FEE_RATE = 0.01
PUMPFUN_FEE_RATE = 0.01
# Set by env to match the user's current expected per-swap network cost.
NETWORK_FEE_USD = {
    "sol": max(0.0, float(os.getenv("PAPER_NETWORK_FEE_SOL_USD", "0"))),
    "eth": max(0.0, float(os.getenv("PAPER_NETWORK_FEE_ETH_USD", "0"))),
}

def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY CHECK (id=1),
            cash REAL NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS positions (
            chain TEXT NOT NULL, mint TEXT NOT NULL, name TEXT NOT NULL,
            symbol TEXT NOT NULL, qty REAL NOT NULL, cost REAL NOT NULL,
            avg_entry REAL NOT NULL, opened_at REAL NOT NULL,
            PRIMARY KEY(chain,mint)
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
            chain TEXT NOT NULL, mint TEXT NOT NULL, name TEXT NOT NULL,
            symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL,
            spot REAL NOT NULL, execution REAL NOT NULL, notional REAL NOT NULL,
            gmgn_fee REAL NOT NULL, platform_fee REAL NOT NULL,
            network_fee REAL NOT NULL, impact_pct REAL NOT NULL,
            realized_pnl REAL
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    con.execute("INSERT OR IGNORE INTO account(id,cash,created_at) VALUES(1,?,?)",
                (INITIAL_USDT, time.time()))
    con.commit()
    return con

@contextmanager
def _db():
    con = _connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def _e(value):
    return html.escape(str(value), quote=True)

def _money(value):
    return f"US$ {float(value):,.2f}"

def _chain(value):
    v = str(value).strip().lower()
    aliases = {"sol": "sol", "solana": "sol", "eth": "eth", "ethereum": "eth"}
    if v not in aliases:
        raise ValueError("Rede inválida. Use SOL ou ETH.")
    return aliases[v]

def get_update_offset():
    with _db() as con:
        row = con.execute("SELECT value FROM settings WHERE key='telegram_update_offset'").fetchone()
        return int(row["value"]) if row else None

def set_update_offset(value):
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('telegram_update_offset',?)",
                    (str(int(value)),))

def _impact(notional, liquidity):
    # Approximation for a balanced constant-product pool: quote reserve ~= half
    # of total USD liquidity. This is intentionally labelled an estimate.
    reserve = float(liquidity) / 2.0
    if reserve <= 0:
        raise ValueError("A GMGN não informou liquidez válida; não abri a simulação.")
    return reserve * float(notional) / (reserve + float(notional))

def buy(chain, mint, amount, quote):
    chain = _chain(chain)
    amount = float(amount)
    mint = str(mint).strip()
    if not mint or amount <= 0:
        raise ValueError("Informe contrato e valor positivo em USDT.")
    price, liquidity = float(quote.get("price") or 0), float(quote.get("liquidity") or 0)
    if price <= 0 or liquidity <= 0:
        raise ValueError("Preço ou liquidez da GMGN indisponível; ordem não simulada.")
    if str(quote.get("chain", chain)) != chain:
        raise ValueError("A cotação retornada não corresponde à rede informada.")
    gmgn_fee = amount * GMGN_FEE_RATE
    launchpad = str(quote.get("launchpad") or "").lower().replace(".", "").replace("-", "").replace("_", "").replace(" ", "")
    platform_rate = PUMPFUN_FEE_RATE if chain == "sol" and "pumpfun" in launchpad else 0.0
    platform_fee = amount * platform_rate
    network_fee = NETWORK_FEE_USD[chain]
    usable = amount - gmgn_fee - platform_fee - network_fee
    if usable <= 0:
        raise ValueError("O valor é insuficiente para cobrir as taxas estimadas.")
    received_usd = _impact(usable, liquidity)
    qty = received_usd / price
    execution = usable / qty
    impact_pct = max(0.0, (execution / price - 1.0) * 100.0)
    with _db() as con:
        account = con.execute("SELECT cash FROM account WHERE id=1").fetchone()
        if amount > float(account["cash"]):
            raise ValueError(f"Saldo fictício insuficiente. Disponível: {_money(account['cash'])}.")
        old = con.execute("SELECT 1 FROM positions WHERE chain=? AND mint=?", (chain,mint)).fetchone()
        if old:
            raise ValueError("Você já tem uma posição aberta nesse contrato. Venda antes de comprar novamente.")
        now = time.time()
        con.execute("UPDATE account SET cash=cash-? WHERE id=1", (amount,))
        con.execute("""INSERT INTO positions VALUES(?,?,?,?,?,?,?,?)""",
                    (chain,mint,str(quote.get("name") or "Token"),str(quote.get("symbol") or "???"),
                     qty,amount,execution,now))
        con.execute("""INSERT INTO trades(at,chain,mint,name,symbol,side,qty,spot,execution,notional,
                       gmgn_fee,platform_fee,network_fee,impact_pct,realized_pnl)
                       VALUES(?,?,?,?,?,'BUY',?,?,?,?,?,?,?,?,NULL)""",
                    (now,chain,mint,str(quote.get("name") or "Token"),str(quote.get("symbol") or "???"),
                     qty,price,execution,amount,gmgn_fee,platform_fee,network_fee,impact_pct))
    return (
        f"🟢 <b>COMPRA SIMULADA</b> — nenhuma transação enviada\n"
        f"🪙 {_e(quote.get('name') or 'Token')} ({_e(quote.get('symbol') or '???')}) • {chain.upper()}\n"
        f"💵 Valor debitado: {_money(amount)} | quantidade estimada: {qty:.8g}\n"
        f"📍 Preço GMGN: {_money(price)} | execução estimada: {_money(execution)}\n"
        f"📉 Impacto modelado: {impact_pct:.2f}%\n"
        f"Taxas estimadas: GMGN {_money(gmgn_fee)}"
        + (f" + plataforma {_money(platform_fee)}" if platform_fee else "")
        + (f" + rede {_money(network_fee)}" if network_fee else " + rede não configurada")
        + "\n⚠️ Fill aproximado por liquidez; rota e execução real podem diferir."
    )

def sell(chain, mint, quote, percent=100.0):
    chain = _chain(chain)
    mint = str(mint).strip()
    percent = float(percent)
    if not 0 < percent <= 100:
        raise ValueError("Percentual de venda deve ser maior que 0 e até 100.")
    price, liquidity = float(quote.get("price") or 0), float(quote.get("liquidity") or 0)
    if price <= 0 or liquidity <= 0:
        raise ValueError("Preço ou liquidez da GMGN indisponível; ordem não simulada.")
    with _db() as con:
        position = con.execute("SELECT * FROM positions WHERE chain=? AND mint=?", (chain,mint)).fetchone()
        if not position:
            raise ValueError("Não encontrei posição aberta para esse contrato e rede.")
        fraction = percent / 100.0
        qty = float(position["qty"]) * fraction
        cost = float(position["cost"]) * fraction
        spot_notional = qty * price
        gross = _impact(spot_notional, liquidity)
        execution = gross / qty
        gmgn_fee = gross * GMGN_FEE_RATE
        platform_fee = gross * PUMPFUN_FEE_RATE if chain == "sol" and "pumpfun" in str(quote.get("launchpad") or "").lower().replace(".", "").replace("-", "").replace("_", "").replace(" ", "") else 0.0
        network_fee = NETWORK_FEE_USD[chain]
        proceeds = gross - gmgn_fee - platform_fee - network_fee
        if proceeds < 0:
            raise ValueError("A venda não cobre as taxas estimadas; nenhuma posição foi alterada.")
        pnl = proceeds - cost
        now = time.time()
        con.execute("UPDATE account SET cash=cash+? WHERE id=1", (proceeds,))
        if percent >= 99.999:
            con.execute("DELETE FROM positions WHERE chain=? AND mint=?", (chain,mint))
        else:
            con.execute("UPDATE positions SET qty=qty-?, cost=cost-? WHERE chain=? AND mint=?",
                        (qty,cost,chain,mint))
        con.execute("""INSERT INTO trades(at,chain,mint,name,symbol,side,qty,spot,execution,notional,
                       gmgn_fee,platform_fee,network_fee,impact_pct,realized_pnl)
                       VALUES(?,?,?,?,?,'SELL',?,?,?,?,?,?,?,?,?)""",
                    (now,chain,mint,position["name"],position["symbol"],qty,price,execution,gross,
                     gmgn_fee,platform_fee,network_fee,max(0.0,(price/execution-1)*100),pnl))
    return (
        f"🔴 <b>VENDA SIMULADA</b> — nenhuma transação enviada\n"
        f"🪙 {_e(position['name'])} ({_e(position['symbol'])}) • {chain.upper()} • {percent:g}%\n"
        f"💵 Valor líquido estimado: {_money(proceeds)} | P&amp;L realizado: <b>{_money(pnl)}</b>\n"
        f"📍 Preço GMGN: {_money(price)} | execução estimada: {_money(execution)}\n"
        f"Taxas estimadas: GMGN {_money(gmgn_fee)}"
        + (f" + plataforma {_money(platform_fee)}" if platform_fee else "")
        + (f" + rede {_money(network_fee)}" if network_fee else " + rede não configurada")
        + "\n⚠️ Fill aproximado por liquidez; rota e execução real podem diferir."
    )

def portfolio(quoter):
    with _db() as con:
        cash = float(con.execute("SELECT cash FROM account WHERE id=1").fetchone()["cash"])
        positions = con.execute("SELECT * FROM positions ORDER BY opened_at").fetchall()
    lines = [f"💼 <b>CARTEIRA SIMULADA</b>", f"💵 Caixa: {_money(cash)}"]
    total = cash
    for p in positions:
        try:
            q = quoter(p["chain"], p["mint"])
            price, liquidity = float(q.get("price") or 0), float(q.get("liquidity") or 0)
            if price <= 0 or liquidity <= 0:
                raise ValueError("cotação incompleta")
            mark = float(p["qty"]) * price
            liq_value = _impact(mark, liquidity)
            fees = liq_value * (GMGN_FEE_RATE + (PUMPFUN_FEE_RATE if p["chain"]=="sol" and "pumpfun" in str(q.get("launchpad") or "").lower().replace(".", "").replace("-", "").replace("_", "").replace(" ", "") else 0))
            net = liq_value - fees - NETWORK_FEE_USD[p["chain"]]
            pnl = net - float(p["cost"])
            total += net
            lines.append(f"• {_e(p['symbol'])} ({p['chain'].upper()}): posição liquidável ~{_money(net)}; P&amp;L ~{_money(pnl)}")
        except Exception:
            lines.append(f"• {_e(p['symbol'])} ({p['chain'].upper()}): cotação indisponível; não marcada a mercado")
    lines.extend([f"📊 Patrimônio estimado: {_money(total)}", "ℹ️ P&amp;L considera saída estimada, taxas GMGN e impacto aproximado."])
    return "\n".join(lines)

def history(limit=10):
    with _db() as con:
        rows = con.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    if not rows:
        return "📒 Ainda não há ordens simuladas."
    lines = ["📒 <b>ÚLTIMAS ORDENS SIMULADAS</b>"]
    for r in rows:
        stamp = time.strftime("%d/%m %H:%M:%S", time.gmtime(r["at"]))
        pnl = f" • P&amp;L {_money(r['realized_pnl'])}" if r["realized_pnl"] is not None else ""
        lines.append(f"{stamp} UTC • {r['side']} { _e(r['symbol']) } {r['chain'].upper()} • {r['qty']:.6g} • execução {_money(r['execution'])}{pnl}")
    return "\n".join(lines)

def reset(amount):
    amount = float(amount)
    if amount <= 0:
        raise ValueError("Saldo deve ser maior que zero.")
    with _db() as con:
        count = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        if count:
            raise ValueError("Feche todas as posições simuladas antes de reiniciar o saldo.")
        con.execute("UPDATE account SET cash=? WHERE id=1", (amount,))
    return f"🔄 Carteira reiniciada com {_money(amount)} fictícios. Histórico anterior preservado."

def help_text():
    return (
        "🧪 <b>SIMULADOR GMGN — carteira fictícia em USDT</b>\n"
        "Usa preço/liquidez atuais da GMGN. Ordens apenas locais; nenhuma transação é enviada.\n\n"
        "<code>/paper saldo</code> — caixa e posições\n"
        "<code>/paper comprar SOL|ETH CONTRATO VALOR_USDT</code>\n"
        "<code>/paper vender SOL|ETH CONTRATO [PERCENTUAL]</code> — padrão 100%\n"
        "<code>/paper histórico</code> — últimas 10 ordens\n"
        "<code>/paper reset VALOR_USDT</code> — só sem posições abertas\n\n"
        "Aplicamos 1% GMGN por swap e 1% Pump.fun quando os metadados indicam essa origem. "
        "Taxa de rede fica como não configurada até definir a estimativa do seu caso. "
        "Impacto no preço é aproximação por liquidez total/2 (modelo constant-product), "
        "não é cotação de rota executável."
    )
