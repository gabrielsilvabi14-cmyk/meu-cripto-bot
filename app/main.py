"""Public read-only dashboard; automated PAPER trading only. No wallet or order endpoints."""
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.integrations.gmgn import fetch_rank, fetch_token_price
from app.trading.paper import PaperAccount

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="GMGN Trader Paper", docs_url=None, redoc_url=None, openapi_url=None)
BASE = Path(__file__).resolve().parent
app.mount("/assets", StaticFiles(directory=BASE / "static"), name="assets")
LOCK = threading.RLock()
ACCOUNT = PaperAccount()
INITIAL_CAPITAL = 100.0
STATE_FILE = Path(os.getenv("PAPER_STATE_FILE", "/tmp/gmgn_paper_state.json"))
MARKET = {"connected": False, "last_check": None, "last_success": None, "errors": {},
          "tokens": [], "cycles": 0, "phase": "iniciando", "chain_status": {}, "paper_enabled": True, "paper_note": "Simulação com preços indicativos, taxas e slippage estimados."}
LAST_PRICES = {}
RADAR_PROCESS = None
RADAR_SIGNAL_FILE = Path(os.getenv('RADAR_SIGNAL_FILE','/tmp/gmgn_radar_signals.jsonl'))
RADAR_SIGNAL_OFFSET = 0


def valid_price(v):
    try:
        f = float(v)
        return f if 0 < f < 1e12 and f == f else None
    except (ValueError, TypeError, OverflowError):
        return None

def restore():
    """Restore a conservative journal snapshot when present; no secrets stored."""
    try:
        from app.trading.paper import Position
        d = json.loads(STATE_FILE.read_text())
        global INITIAL_CAPITAL
        INITIAL_CAPITAL = float(d.get("initial_capital", 100.0))
        ACCOUNT.cash_usd = float(d["cash"])
        ACCOUNT.positions = {k: Position(**v) for k, v in d["positions"].items()}
        ACCOUNT.events = d["events"][-200:]
    except (OSError, ValueError, KeyError, TypeError):
        pass

def save():
    from dataclasses import asdict
    data = {"initial_capital": INITIAL_CAPITAL, "cash": ACCOUNT.cash_usd,
            "positions": {k: asdict(v) for k, v in ACCOUNT.positions.items()},
            "events": ACCOUNT.events[-200:]}
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(STATE_FILE)
    except OSError as exc:
        logging.warning("Paper state not persisted: %s", type(exc).__name__)

def pumpfun_confirmed(token):
    """Ranking alone cannot establish Pump.fun provenance; do not trade SOL without proof."""
    return str(token.get("source") or "").lower() in ("pump.fun", "pumpfun")

def eligible(t):
    """Conservative Telegram Radar-inspired paper entries; unknown data cannot pass."""
    price = valid_price(t.get("price"))
    liquidity = valid_price(t.get("liquidity"))
    volume = valid_price(t.get("volume_1h"))
    buys = t.get("buys_1h")
    sells = t.get("sells_1h")
    change = t.get("change_1h")
    age = t.get("age_hours")
    if not all((price, liquidity, volume)):
        return False
    if t.get("chain") == "sol":
        # No simulated SOL purchase until Pump.fun origin is verified.
        return False
    if t.get("chain") != "eth":
        return False
    if liquidity < 20000 or volume < 30000 or volume / liquidity > 20:
        return False
    if buys is None or sells is None or change is None or age is None:
        return False
    if buys < 30 or sells < 10 or buys < sells * 1.15:
        return False
    if age < 10 / 60 or age > 24:
        return False
    if change < 3 or change > 65:
        return False
    return True

def poll():
    restore()
    logging.info("GMGN paper worker started; key configured=%s", bool(os.getenv("GMGN_API_KEY")))
    while True:
        tokens, errors, chain_status = [], {}, {} 
        with LOCK:
            MARKET["phase"] = "consultando GMGN"
            MARKET["last_check"] = datetime.now(timezone.utc).isoformat()
        for chain in ("sol", "eth"):
            try:
                found = fetch_rank(chain, limit=20)
                tokens.extend(found)
                chain_status[chain] = {"ok": True, "tokens": len(found)}
                logging.info("GMGN %s returned %s tokens", chain, len(found))
            except Exception as exc:
                errors[chain] = str(exc)[:160]
                chain_status[chain] = {"ok": False, "error": errors[chain]}
                logging.warning("GMGN %s failed: %s", chain, errors[chain])
            time.sleep(6)
        now = datetime.now(timezone.utc).isoformat()
        with LOCK:
            MARKET.update(connected=bool(tokens), last_check=now, errors=errors,
                          chain_status=chain_status, phase=("monitorando" if tokens else "erro na integração"),
                          tokens=tokens[:40], cycles=MARKET["cycles"] + 1)
            if tokens:
                MARKET["last_success"] = now
            current = {}
            for t in tokens:
                price = valid_price(t.get("price"))
                if price:
                    current[t["chain"] + ":" + t["address"]] = price
            for key in list(ACCOUNT.positions):
                if key not in current:
                    chain, address = key.split(':', 1)
                    price = fetch_token_price(chain, address)
                    if price:
                        current[key] = price
                    time.sleep(2)
            LAST_PRICES.update(current)
            # Exits only when a fresh, valid price is available for this cycle.
            for key in list(ACCOUNT.positions):
                if key in current:
                    try:
                        ACCOUNT.evaluate_exit(key, current[key])
                    except (ValueError, KeyError) as exc:
                        logging.warning("Paper exit skipped: %s", type(exc).__name__)
            # Conservative Telegram-inspired screening;
            save()

        time.sleep(288)



def signal_worker():
    """Read Telegram-approved signals promptly and explain every paper decision."""
    global RADAR_SIGNAL_OFFSET
    logging.info("Paper signal consumer started")
    while True:
        try:
            with RADAR_SIGNAL_FILE.open("r",encoding="utf-8") as journal:
                journal.seek(RADAR_SIGNAL_OFFSET)
                lines=journal.readlines()
                RADAR_SIGNAL_OFFSET=journal.tell()
            for line in lines:
                try:
                    signal=json.loads(line)
                    chain=signal["chain"]
                    key=chain+":"+signal["address"]
                    price=valid_price(signal.get("price"))
                    liquidity=valid_price(signal.get("liquidity"))
                    reason=None
                    with LOCK:
                        if time.time()-float(signal.get("timestamp",0))>1800:
                            reason="sinal expirado"
                        elif chain not in ("sol","eth"):
                            reason="rede não habilitada"
                        elif chain=="sol" and not signal.get("pumpfun_confirmed"):
                            reason="origem Pump.fun não confirmada"
                        elif not price:
                            reason="preço inválido"
                        elif not liquidity:
                            reason="liquidez inválida"
                        elif key in ACCOUNT.positions:
                            reason="posição já aberta"
                        else:
                            try:
                                event=ACCOUNT.buy(key,chain,price,liquidity)
                                LAST_PRICES[key]=price
                                MARKET.setdefault("paper_decisions",[]).append({"token":key,"result":"comprado","reason":"Radar aprovado","timestamp":datetime.now(timezone.utc).isoformat()})
                                logging.info("PAPER BUY from Telegram Radar %s: %s",key,event)
                                save()
                            except (ValueError,KeyError) as exc:
                                reason=str(exc)
                        if reason:
                            logging.info("PAPER SKIP %s: %s",key,reason)
                            MARKET.setdefault("paper_decisions",[]).append({"token":key,"result":"ignorado","reason":reason,"timestamp":datetime.now(timezone.utc).isoformat()})
                        MARKET["paper_decisions"]=MARKET.get("paper_decisions",[])[-80:]
                except (ValueError,KeyError,TypeError) as exc:
                    logging.warning("Malformed radar signal: %s",type(exc).__name__)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logging.warning("Signal journal unavailable: %s",type(exc).__name__)
        time.sleep(2)

@app.on_event("startup")
def startup():
    global RADAR_PROCESS
    if all(os.getenv(k) for k in ("TELEGRAM_TOKEN","TELEGRAM_CHAT_ID","GMGN_API_KEY")):
        env=dict(os.environ)
        env["RADAR_EMBEDDED"]="1"
        RADAR_PROCESS=subprocess.Popen([sys.executable,str(BASE.parent/"telegram_radar.py")],env=env)
    else:
        logging.warning("Telegram Radar inactive: configure TELEGRAM_TOKEN and TELEGRAM_CHAT_ID")
    threading.Thread(target=poll, daemon=True, name="gmgn-paper").start()
    threading.Thread(target=signal_worker, daemon=True, name="radar-signals").start()

@app.middleware("http")
async def headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    return response

@app.get("/health")
def health():
    return {"status": "online", "mode": "paper", "real_trading": False}

@app.get("/login")
def old_login_redirect():
    return RedirectResponse("/", status_code=303)

@app.get("/", response_class=HTMLResponse)
def home():
    return (BASE / "dashboard.html").read_text(encoding="utf-8")

@app.get("/api/status")
def status():
    with LOCK:
        positions = []
        equity = ACCOUNT.cash_usd
        for key, p in ACCOUNT.positions.items():
            mark = LAST_PRICES.get(key)
            value = p.quantity * mark if mark else None
            if value is not None:
                equity += value
            positions.append({"token": key, "network": p.network, "quantity": p.quantity,
                              "entry_price": p.entry_price, "mark_price": mark,
                              "estimated_value_usd": value, "cost_basis_usd": p.entry_cost_usd})
        token_meta = {t["chain"] + ":" + t["address"]: {"symbol": t.get("symbol"), "name": t.get("name")} for t in MARKET["tokens"]}
        for p in positions:
            p.update(token_meta.get(p["token"], {}))
            if p["mark_price"] and p["entry_price"]:
                p["change_pct"] = round((p["mark_price"] / p["entry_price"] - 1) * 100, 2)
                p["unrealized_pnl_usd"] = round(p["estimated_value_usd"] - p["cost_basis_usd"], 4)
            p["stop_price"] = p["entry_price"] * (1 - ACCOUNT.config.stop_pct / 100)
        return {"mode": "paper", "connected_to_gmgn": MARKET["connected"],
                "trading_enabled": False, "paper_enabled": True, "networks": ["sol", "eth"], "telegram_radar_running": RADAR_PROCESS is not None and RADAR_PROCESS.poll() is None,
                "planned_risk_pct": 2, "initial_paper_capital_usd": INITIAL_CAPITAL,
                "cash_usd": round(ACCOUNT.cash_usd, 4),
                "estimated_equity_usd": round(equity, 4) if len(positions) == sum(p["estimated_value_usd"] is not None for p in positions) else None,
                "positions": positions, "trades": ACCOUNT.events[-30:],
                "market": dict(MARKET), "paper_decisions": MARKET.get("paper_decisions",[])[-30:], "timestamp": datetime.now(timezone.utc).isoformat(),
                "warning": "Dados indicativos; liquidez, preço e execução não garantidos. Estado local pode ser perdido ao reiniciar o Render."}

@app.post("/api/paper/reset")
async def reset_paper(request: Request):
    """Confirmed reset of the public paper portfolio."""
    global INITIAL_CAPITAL
    origin = request.headers.get("origin", "")
    host = request.headers.get("host", "")
    if not origin or origin not in ("https://" + host, "http://" + host):
        return JSONResponse({"error": "Origem não autorizada"}, status_code=403)
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return JSONResponse({"error": "JSON obrigatório"}, status_code=415)
    if int(request.headers.get("content-length") or "0") > 1024:
        return JSONResponse({"error": "Dados muito grandes"}, status_code=413)
    try:
        payload = await request.json()
        amount = float(payload.get("capital_usd"))
        confirmed = payload.get("confirm_reset") is True
        from math import isfinite
        if not confirmed or not isfinite(amount) or amount < 10 or amount > 100000:
            raise ValueError("Capital inválido ou redefinição não confirmada")
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"error": "Informe um capital entre US$ 10 e US$ 100.000 e confirme a redefinição"}, status_code=400)
    with LOCK:
        INITIAL_CAPITAL = round(amount, 2)
        ACCOUNT.cash_usd = INITIAL_CAPITAL
        ACCOUNT.positions.clear()
        ACCOUNT.events.clear()
        save()
    return {"ok": True, "initial_paper_capital_usd": INITIAL_CAPITAL,
            "notice": "Carteira fictícia reiniciada; operações anteriores apagadas"}
