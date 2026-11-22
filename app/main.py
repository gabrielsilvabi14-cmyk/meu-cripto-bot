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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.integrations.gmgn import fetch_rank, fetch_token_price, fetch_token_market_cap, fetch_market_observation
from app.trading.paper import PaperAccount
from app.trading.adaptive import evaluate as evaluate_learning
from app.integrations.candles import fetch_minute_candles, classify as classify_candles

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="GMGN Trader Paper", docs_url=None, redoc_url=None, openapi_url=None)
BASE = Path(__file__).resolve().parent
app.mount("/assets", StaticFiles(directory=BASE / "static"), name="assets")
LOCK = threading.RLock()
ACCOUNT = PaperAccount()
PAPER_SETTINGS = {"operation_usd":20.0,"stop_pct":0.0,"target_pct":0.0}
INITIAL_CAPITAL = 100.0
EXTERNAL_FUNDING = 0.0
STATE_FILE = Path(os.getenv("PAPER_STATE_FILE", "/tmp/gmgn_paper_state.json"))
MARKET = {"connected": False, "last_check": None, "last_success": None, "errors": {},
          "tokens": [], "cycles": 0, "phase": "iniciando", "chain_status": {}, "paper_enabled": True, "paper_note": "Simulação com preços indicativos, taxas e slippage estimados."}
LAST_PRICES = {}
LAST_MARKET_CAPS = {}
RADAR_PROCESS = None
RADAR_SIGNAL_FILE = Path(os.getenv('RADAR_SIGNAL_FILE','/tmp/gmgn_radar_signals.jsonl'))
RADAR_SIGNAL_OFFSET = 0
SIGNAL_STATS = {"received":0,"bought":0,"skipped":0,"last_error":None,"last_signal":None}


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
        global INITIAL_CAPITAL, EXTERNAL_FUNDING
        # Legacy snapshots increased initial_capital on automatic deposits.
        EXTERNAL_FUNDING = float(d.get("external_funding_usd", max(0.0, float(d.get("initial_capital", 100.0)) - 100.0)))
        INITIAL_CAPITAL = 100.0
        if "operation_usd" in d.get("paper_settings", {}):
            PAPER_SETTINGS["operation_usd"] = float(d["paper_settings"]["operation_usd"])
        ACCOUNT.cash_usd = float(d["cash"])
        ACCOUNT.positions = {k: Position(**v) for k, v in d["positions"].items()}
        ACCOUNT.events = d["events"]
    except (OSError, ValueError, KeyError, TypeError):
        pass

def save():
    from dataclasses import asdict
    data = {"initial_capital": INITIAL_CAPITAL, "external_funding_usd": EXTERNAL_FUNDING, "cash": ACCOUNT.cash_usd,
            "positions": {k: asdict(v) for k, v in ACCOUNT.positions.items()},
            "events": ACCOUNT.events, "paper_settings":dict(PAPER_SETTINGS)}
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
        cycle_started = time.monotonic()
        tokens, errors, chain_status = [], {}, {} 
        with LOCK:
            MARKET["phase"] = "consultando GMGN"
            MARKET["last_check"] = datetime.now(timezone.utc).isoformat()
        for chain in ("sol",):
            try:
                found = fetch_rank(chain, limit=100)
                tokens.extend(found)
                chain_status[chain] = {"ok": True, "tokens": len(found)}
                logging.info("GMGN %s returned %s tokens", chain, len(found))
            except Exception as exc:
                errors[chain] = str(exc)[:160]
                chain_status[chain] = {"ok": False, "error": errors[chain]}
                logging.warning("GMGN %s failed: %s", chain, errors[chain])
        now = datetime.now(timezone.utc).isoformat()
        with LOCK:
            MARKET.update(connected=bool(tokens), last_check=now, errors=errors,
                          chain_status=chain_status, phase=("monitorando" if tokens else "erro na integração"),
                          tokens=tokens[:100], cycles=MARKET["cycles"] + 1)
            if tokens:
                MARKET["last_success"] = now
            current = {}
            for t in tokens:
                price = valid_price(t.get("price"))
                if price:
                    current[t["chain"] + ":" + t["address"]] = price
            # Network I/O must happen outside LOCK: a separate exit worker
            # monitors open positions frequently and independently.
            LAST_PRICES.update(current)
            for t in tokens:
                mc=valid_price(t.get("market_cap"))
                if mc: LAST_MARKET_CAPS[t["chain"]+":"+t["address"]]=mc
            # Conservative Telegram-inspired screening;
            save()

        elapsed = time.monotonic() - cycle_started
        logging.info("GMGN RANK cycle finished elapsed=%.1fs tokens=%s next_in=%.1fs", elapsed, len(tokens), max(0, 300 - elapsed))
        time.sleep(max(0, 300 - elapsed))



def monitor_exits():
    """Check live indicative spot prices repeatedly; no candle-close dependency."""
    interval = 60.0
    while True:
        cycle_started = time.monotonic()
        try:
            with LOCK:
                keys = list(ACCOUNT.positions)
            for key in keys:
                try:
                    chain, address = key.split(":", 1)
                    observation = fetch_market_observation(chain, address)
                    price = valid_price(observation.get("price")) if observation else None
                    if price is None:
                        logging.warning("PAPER MARK unavailable %s; adaptive exit not evaluated, position remains unprotected", key)
                        continue
                    candle_patterns = classify_candles(fetch_minute_candles(address)) if chain == 'sol' else []
                    if candle_patterns:
                        observation = dict(observation or {}, candle_patterns=candle_patterns)
                    with LOCK:
                        LAST_PRICES[key] = price
                        if key in ACCOUNT.positions:
                            event = ACCOUNT.evaluate_exit(key, price, observation)
                            save()  # Persist adaptive trailing state on every observation.
                            if event:
                                logging.info("PAPER EXIT %s reason=%s pnl_usd=%s pnl_pct=%s entry=%s exit=%s exit_at=%s observed=%s score=%s migration=%s",
                                             key, event.get("exit_reason"), event.get("realized_pnl_usd"),
                                             event.get("realized_pnl_pct"), event.get("entry_price"),
                                             event.get("exit_price"), event.get("timestamp"),
                                             event.get("observed_price"), event.get("entry_score"),
                                             event.get("migration_status"))
                                save()
                except Exception:
                    logging.exception("PAPER EXIT monitor error for %s", key)
        except Exception:
            logging.exception("PAPER EXIT monitor loop error")
        elapsed = time.monotonic() - cycle_started
        wait_seconds = max(0, interval - elapsed)
        logging.info("PAPER EXIT spot cycle finished positions=%s elapsed=%.1fs next_in=%.1fs",
                     len(keys) if "keys" in locals() else 0, elapsed, wait_seconds)
        time.sleep(wait_seconds)


def buy_every_alert(key,chain,price,market_cap=None,score=None,migration_status="nao_verificada",migration_age_minutes=None,entry_patterns=None):
    """Independent $20 paper position for each alerted token; virtual funding is explicit."""
    from app.trading.paper import Position
    notional=PAPER_SETTINGS["operation_usd"]
    fee=notional*ACCOUNT.fee_pct/100
    required=notional+fee
    # Never silently inject fictional funds. Starting balance remains US$100.
    if ACCOUNT.cash_usd < required:
        raise ValueError("saldo fictício insuficiente; nenhuma compra ou depósito automático")
    execution_price=price*(1+ACCOUNT.slippage_pct/100)
    ACCOUNT.cash_usd-=required
    ACCOUNT.positions[key]=Position(key,chain,notional/execution_price,execution_price,required,datetime.now(timezone.utc).isoformat(),entry_market_cap=market_cap,stop_pct=PAPER_SETTINGS["stop_pct"],target_pct=PAPER_SETTINGS["target_pct"],entry_score=score,migration_status=migration_status,migration_age_minutes=migration_age_minutes,entry_patterns=list(entry_patterns or []))
    event={"action":"PAPER_BUY","token":key,"network":chain,"notional_usd":notional,"fee_usd":fee,"entry_price":execution_price,"entry_patterns":list(entry_patterns or []),"timestamp":datetime.now(timezone.utc).isoformat()}
    ACCOUNT.events.append(event)
    return event

def assess_paper_entry(signal, candles, observation):
    """Require current, coherent market data and completed-candle confirmation."""
    quote = valid_price((observation or {}).get("price"))
    signal_price = valid_price(signal.get("price"))
    liquidity = valid_price((observation or {}).get("liquidity"))
    if not quote or not signal_price:
        return None, "cotação atual indisponível; entrada adiada"
    if abs(quote / signal_price - 1) > 0.12:
        return None, "cotação mudou mais de 12% desde o sinal"
    if liquidity is None or liquidity < 12000:
        return None, "liquidez atual insuficiente ou não verificada (mínimo US$ 12 mil)"
    if len(candles) < 8 or any(b["ts"] - a["ts"] != 60 for a, b in zip(candles[-8:], candles[-7:])):
        return None, "histórico OHLCV de 8 velas fechadas indisponível"
    recent = candles[-8:]
    closes = [x["close"] for x in recent]
    vols = [x["volume"] for x in recent]
    if closes[-1] <= 0 or min(closes) <= 0:
        return None, "velas com preços inválidos"
    if abs(quote / closes[-1] - 1) > 0.18:
        return None, "preço da pool OHLCV diverge da cotação atual"
    if closes[-1] < closes[-3] or closes[-1] < closes[-2]:
        return None, "momentum de curto prazo negativo"
    if closes[-1] < max(x["high"] for x in recent) * 0.84:
        return None, "recuo acentuado do pico recente"
    if closes[-1] > closes[0] * 1.8 or closes[-1] > closes[-2] * 1.20:
        return None, "alta explosiva; risco de entrada tardia"
    if sum(vols[-3:]) < sum(vols[:3]) * 0.75:
        return None, "volume recente enfraquecendo"
    buy_vol = (observation or {}).get("buy_volume_1m")
    sell_vol = (observation or {}).get("sell_volume_1m")
    if isinstance(buy_vol, (int, float)) and isinstance(sell_vol, (int, float)) and sell_vol > max(buy_vol, 1) * 1.4:
        return None, "pressão vendedora atual"
    return quote, None


def swing_week_worker():
    """Independent 1-7 day Solana paper strategy; migration is not an entry gate."""
    while True:
        started = time.monotonic()
        checked = 0
        qualified = 0
        bought = 0
        try:
            from app.integrations.gmgn import fetch_token_snapshot
            candidates = fetch_rank("sol", interval="1h", limit=100)
            for t in candidates:
                age = valid_price(t.get("age_hours"))
                if age is None or not 24 <= age <= 168:
                    continue
                checked += 1
                address = t.get("address")
                if not address:
                    continue
                snapshot = fetch_token_snapshot("sol", address)
                # launchpad_status may be a numeric migration state, not provenance.
                # Swing7 paper entries do not require migration or launchpad status.
                origin = str((snapshot or {}).get("launchpad_status") or "").lower()
                if origin not in ("pump.fun", "pumpfun", "pump_fun"):
                    logging.info("SWING7 INFO sol:%s pumpfun_origin_unverified status=%s; paper evaluation continues", address[:8], origin[:32])
                liquidity = valid_price((snapshot or {}).get("liquidity"))
                price = valid_price((snapshot or {}).get("price"))
                if not liquidity or liquidity < 20000 or not price:
                    logging.info("SWING7 SKIP sol:%s liquidity_or_price_unavailable", address[:8])
                    continue
                candles = fetch_minute_candles(address)
                patterns = classify_candles(candles)
                signal = {"price": price}
                quote, issue = assess_paper_entry(signal, candles, snapshot)
                reversal = "reversao_apos_queda" in patterns
                positive = {"rompimento_com_volume", "acumulacao_gradual_volume_crescente", "reversao_apos_queda"}
                if issue or not positive.intersection(patterns) or "venda_forte_volume_elevado" in patterns or ("queda_25pct_do_pico" in patterns and not reversal):
                    logging.info("SWING7 SKIP sol:%s reason=%s patterns=%s", address[:8], issue or "no_positive_pattern", patterns)
                    continue
                qualified += 1
                score = 85 if "rompimento_com_volume" in patterns else (80 if reversal else 75)
                key = "sol:" + address
                with LOCK:
                    if key in ACCOUNT.positions:
                        logging.info("SWING7 SKIP %s position_already_open", key)
                        continue
                    recent_buy = False
                    for past in reversed(ACCOUNT.events):
                        if past.get("action") != "PAPER_BUY" or past.get("token") != key:
                            continue
                        try:
                            purchased_at = datetime.fromisoformat(past["timestamp"].replace("Z", "+00:00"))
                            recent_buy = (datetime.now(timezone.utc) - purchased_at).total_seconds() < 3600
                        except (KeyError, TypeError, ValueError):
                            recent_buy = True
                        break
                    if recent_buy:
                        logging.info("SWING7 SKIP %s one_hour_reentry_cooldown", key)
                        continue
                    if score <= evaluate_learning(ACCOUNT.events)["minimum_score_exclusive"]:
                        logging.info("SWING7 SKIP %s score_below_adaptive_minimum score=%s", key, score)
                        continue
                    try:
                        event = buy_every_alert(key, "sol", quote, valid_price(t.get("market_cap")), score,
                                                migration_status="not_required_swing7",
                                                migration_age_minutes=None, entry_patterns=patterns)
                        LAST_PRICES[key] = quote
                        bought += 1
                        SIGNAL_STATS["bought"] += 1
                        MARKET.setdefault("paper_decisions", []).append({
                            "token": key, "result": "comprado", "strategy": "swing7",
                            "reason": "Swing 7 dias: liquidez, cotacao e OHLCV validados; origem nao confirmada",
                            "timestamp": datetime.now(timezone.utc).isoformat()})
                        MARKET["paper_decisions"] = MARKET["paper_decisions"][-80:]
                        save()
                        logging.info("SWING7 PAPER BUY %s age_h=%.1f score=%s patterns=%s event=%s", key, age, score, patterns, event)
                    except (ValueError, KeyError) as exc:
                        logging.info("SWING7 SKIP %s reason=%s", key, str(exc))
                logging.info("SWING7 VERIFIED_CANDIDATE sol:%s age_h=%.1f patterns=%s price=%s", address[:8], age, patterns, quote)
            logging.info("SWING7 cycle finished candidates_1to7d=%s verified=%s bought=%s elapsed=%.1fs", checked, qualified, bought, time.monotonic()-started)
        except Exception:
            logging.exception("SWING7 discovery error")
        time.sleep(max(0, 900 - (time.monotonic()-started)))


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
                    SIGNAL_STATS["received"] += 1
                    SIGNAL_STATS["last_signal"] = datetime.now(timezone.utc).isoformat()
                    chain=signal["chain"]
                    key=chain+":"+signal["address"]
                    price=valid_price(signal.get("price"))
                    liquidity=valid_price(signal.get("liquidity"))
                    # External OHLCV request outside the account lock.
                    entry_candles = fetch_minute_candles(signal["address"]) if chain == "sol" else []
                    entry_patterns = classify_candles(entry_candles)
                    observation = fetch_market_observation(chain, signal["address"]) if chain == "sol" else None
                    confirmed_price, entry_issue = assess_paper_entry(signal, entry_candles, observation)
                    reason=None
                    with LOCK:
                        if time.time()-float(signal.get("timestamp",0))>86400:
                            reason="sinal expirado"
                        elif chain != "sol":
                            reason="rede não habilitada"
                        elif not isinstance(signal.get("score"), (int, float)) or isinstance(signal.get("score"), bool) or not (evaluate_learning(ACCOUNT.events)["minimum_score_exclusive"] < signal["score"] <= 100):
                            reason="pontuação insuficiente: exige acima de %s/100 (aprendizado adaptativo)" % evaluate_learning(ACCOUNT.events)["minimum_score_exclusive"]
                        elif not price:
                            reason="preço inválido"
                        elif entry_issue:
                            reason=entry_issue
                        elif key in ACCOUNT.positions:
                            reason="posição já aberta"
                        elif "venda_forte_volume_elevado" in entry_patterns or "queda_25pct_do_pico" in entry_patterns:
                            reason="OHLCV: pressão vendedora ou queda acentuada antes da entrada"
                        else:
                            try:
                                event=buy_every_alert(key,chain,confirmed_price,valid_price(signal.get('market_cap')),signal.get("score"),signal.get("migration_status","nao_verificada"),signal.get("migration_age_minutes"),entry_patterns=entry_patterns)
                                LAST_PRICES[key]=confirmed_price
                                if valid_price(signal.get('market_cap')):
                                    LAST_MARKET_CAPS[key]=valid_price(signal.get('market_cap'))
                                MARKET.setdefault("paper_decisions",[]).append({"token":key,"result":"comprado","reason":"Radar e confirmação de preço, liquidez e velas","timestamp":datetime.now(timezone.utc).isoformat()})
                                SIGNAL_STATS["bought"] += 1
                                logging.info("PAPER BUY from Telegram Radar %s: %s entry_at=%s entry_price=%s migration_status=%s migration_age_minutes=%s score=%s",key,event,event.get("timestamp"),event.get("entry_price"),signal.get("migration_status","nao_verificada"),signal.get("migration_age_minutes"),signal.get("score"))
                                save()
                            except (ValueError,KeyError) as exc:
                                reason=str(exc)
                        if reason:
                            SIGNAL_STATS["skipped"] += 1
                            logging.info("PAPER SKIP %s: %s",key,reason)
                            MARKET.setdefault("paper_decisions",[]).append({"token":key,"result":"ignorado","reason":reason,"timestamp":datetime.now(timezone.utc).isoformat()})
                        MARKET["paper_decisions"]=MARKET.get("paper_decisions",[])[-80:]
                except Exception as exc:
                    SIGNAL_STATS["last_error"] = str(exc)[:200]
                    logging.exception("Paper signal processing FAILED: %s",type(exc).__name__)
        except FileNotFoundError:
            pass
        except Exception as exc:
            SIGNAL_STATS["last_error"] = str(exc)[:200]
            logging.exception("Signal journal processing failed")
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
    threading.Thread(target=swing_week_worker, daemon=True, name="swing-week").start()
    threading.Thread(target=monitor_exits, daemon=True, name="paper-exits").start()

@app.middleware("http")
async def headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    return response

@app.head("/")
def root_head():
    return Response(status_code=200)

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
            value = p.quantity * mark * (1-ACCOUNT.slippage_pct/100) * (1-ACCOUNT.fee_pct/100) if mark else None
            if value is not None:
                equity += value
            positions.append({"token": key, "network": p.network, "quantity": p.quantity,
                              "entry_price": p.entry_price, "mark_price": mark,
                              "entry_market_cap":p.entry_market_cap,"current_market_cap":LAST_MARKET_CAPS.get(key),
                              "estimated_value_usd": value, "cost_basis_usd": p.entry_cost_usd,
                              "last_mark_at":p.last_mark_at,"mark_is_stale":not p.last_mark_at or (datetime.now(timezone.utc)-datetime.fromisoformat(p.last_mark_at)).total_seconds()>60})
        token_meta = {t["chain"] + ":" + t["address"]: {"symbol": t.get("symbol"), "name": t.get("name")} for t in MARKET["tokens"]}
        for p in positions:
            p.update(token_meta.get(p["token"], {}))
            if p["mark_price"] and p["entry_price"]:
                p["change_pct"] = round((p["mark_price"] / p["entry_price"] - 1) * 100, 2)
                p["unrealized_pnl_usd"] = round(p["estimated_value_usd"] - p["cost_basis_usd"], 4)
            paper_pos = ACCOUNT.positions[p["token"]]
            p["stop_pct"] = paper_pos.stop_pct
            p["target_pct"] = paper_pos.target_pct
            p["stop_price"] = paper_pos.dynamic_stop_price
            p["exit_strategy"] = paper_pos.exit_strategy
            p["peak_price"] = paper_pos.peak_price
            p["market_signals"] = paper_pos.last_market_signals
        return {"mode": "paper", "connected_to_gmgn": MARKET["connected"],
                "trading_enabled": False, "paper_enabled": True, "networks": ["sol"], "telegram_radar_running": RADAR_PROCESS is not None and RADAR_PROCESS.poll() is None,
                "planned_risk_pct": 2, "paper_settings":dict(PAPER_SETTINGS), "learning":evaluate_learning(ACCOUNT.events), "signal_stats":dict(SIGNAL_STATS), "initial_paper_capital_usd": INITIAL_CAPITAL, "external_funding_usd": EXTERNAL_FUNDING,
                "cash_usd": round(ACCOUNT.cash_usd, 4),
                "estimated_equity_usd": round(equity, 4) if len(positions) == sum(p["estimated_value_usd"] is not None for p in positions) else None,
                "positions": positions, "trades": ACCOUNT.events,
                "market": dict(MARKET), "paper_decisions": MARKET.get("paper_decisions",[])[-30:], "timestamp": datetime.now(timezone.utc).isoformat(),
                "warning": "Dados indicativos; liquidez, preço e execução não garantidos. Estado local pode ser perdido ao reiniciar o Render."}


@app.post("/api/paper/settings")
async def update_paper_settings(request: Request):
    """Update only the paper buy amount; adaptive exits are managed by the engine."""
    origin=request.headers.get("origin","")
    host=request.headers.get("host","")
    if not origin or origin not in ("https://"+host,"http://"+host):
        return JSONResponse({"error":"Origem não autorizada"},status_code=403)
    if request.headers.get("content-type","").split(";")[0].strip()!="application/json":
        return JSONResponse({"error":"JSON obrigatório"},status_code=415)
    if int(request.headers.get("content-length") or "0")>1024:
        return JSONResponse({"error":"Dados muito grandes"},status_code=413)
    try:
        from math import isfinite
        data=await request.json()
        settings={"operation_usd":float(data["operation_usd"])}
        if not all(isfinite(v) for v in settings.values()):
            raise ValueError()
        if not (1<=settings["operation_usd"]<=10000):
            raise ValueError()
    except (ValueError,TypeError,KeyError,AttributeError):
        return JSONResponse({"error":"Use valor por operação entre US$ 1 e US$ 10.000"},status_code=400)
    with LOCK:
        PAPER_SETTINGS.update(settings)
        save()
    return {"ok":True,"paper_settings":dict(PAPER_SETTINGS),"notice":"Novas compras usarão o valor definido; saídas permanecem automáticas."}

@app.post("/api/paper/reset")
async def reset_paper(request: Request):
    """Confirmed reset of the public paper portfolio."""
    global INITIAL_CAPITAL, EXTERNAL_FUNDING
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
        if not confirmed or not isfinite(amount) or amount != 100.0:
            raise ValueError("Capital inválido ou redefinição não confirmada")
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"error": "O capital inicial é fixo em US$ 100; confirme a redefinição"}, status_code=400)
    with LOCK:
        INITIAL_CAPITAL = 100.0
        EXTERNAL_FUNDING = 0.0
        ACCOUNT.cash_usd = INITIAL_CAPITAL
        ACCOUNT.positions.clear()
        ACCOUNT.events.clear()
        save()
    return {"ok": True, "initial_paper_capital_usd": INITIAL_CAPITAL,
            "notice": "Carteira fictícia reiniciada; operações anteriores apagadas"}
