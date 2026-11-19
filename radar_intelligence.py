"""Radar 4.0: filtros independentes e aprendizado observacional (sem trades)."""
import json
import math
import os
import sqlite3
import time

DB_PATH = os.getenv("RADAR_DB_PATH", "radar_intelligence.sqlite3")
MIN_SCORE = 60
MIN_SCORE_BY_STRATEGY = {"eth_new": 45, "eth_trending": 50}
WINDOWS = (300, 900, 1800, 3600, 10800)
CONFIG = {
    "new": dict(age=(5/60, 3), cap=(30000, 2000000), liq=20000, vol=1500, ratio=1.15, change=(-8, 35)),
    "trending": dict(age=(.5, 48), cap=(100000, 20000000), liq=30000, vol=30000, ratio=1.1, change=(-10, 45)),
    "bsc_new": dict(age=(5/60, 3), cap=(30000, 2000000), liq=25000, vol=1800, ratio=1.15, change=(-8, 35)),
    "bsc_trending": dict(age=(.5, 48), cap=(100000, 20000000), liq=40000, vol=35000, ratio=1.1, change=(-10, 45)),
    # ETH: janela e limites mais flexíveis que SOL, sem remover liquidez,
    # volume, pressão compradora ou o teto de variação contra picos extremos.
    "eth_new": dict(age=(5/60, 72), cap=(8000, 25000000), liq=5000, vol=750, ratio=0.9, change=(-35, 150)),
    "eth_trending": dict(age=(.25, 240), cap=(20000, 100000000), liq=8000, vol=5000, ratio=0.9, change=(-40, 180)),
}

def number(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None

def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS candidates (
        mint TEXT, strategy TEXT, observed INTEGER, score INTEGER,
        eligible INTEGER, reason TEXT, features TEXT,
        PRIMARY KEY(mint,strategy,observed))""")
    con.execute("""CREATE TABLE IF NOT EXISTS cohorts (
        mint TEXT, strategy TEXT, started INTEGER, price REAL,
        features TEXT, PRIMARY KEY(mint,strategy,started))""")
    con.execute("""CREATE TABLE IF NOT EXISTS outcomes (
        mint TEXT, strategy TEXT, started INTEGER, horizon INTEGER,
        observed INTEGER, return_pct REAL, status TEXT,
        PRIMARY KEY(mint,strategy,started,horizon))""")
    con.execute("""CREATE TABLE IF NOT EXISTS path_metrics (
        mint TEXT, strategy TEXT, started INTEGER, horizon INTEGER,
        mfe_pct REAL, mae_pct REAL, net_mfe_pct REAL, net_mae_pct REAL,
        estimated_cost_pct REAL, sample_count INTEGER, status TEXT,
        PRIMARY KEY(mint,strategy,started,horizon))""")
    con.execute("""CREATE TABLE IF NOT EXISTS path_jobs (
        mint TEXT, strategy TEXT, started INTEGER, attempts INTEGER,
        status TEXT, updated INTEGER,
        PRIMARY KEY(mint,strategy,started))""")
    con.execute("""CREATE TABLE IF NOT EXISTS sent_alerts (
        chain TEXT, mint TEXT, sent INTEGER,
        PRIMARY KEY(chain,mint))""")
    return con

def recently_sent(chain, mint, seconds=43200):
    with db() as con:
        row = con.execute("SELECT sent FROM sent_alerts WHERE chain=? AND mint=?", (chain,mint)).fetchone()
    return row is not None and time.time()-row[0] < seconds

def mark_sent(chain, mint):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO sent_alerts VALUES (?,?,?)", (chain,mint,int(time.time())))

def candidate_count():
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]

def features(pair, age):
    tx = pair.get("txns") or {}
    vol = pair.get("volume") or {}
    changes = pair.get("priceChange") or {}
    return {
        "age": age, "cap": number(pair.get("marketCap")),
        "liq": number((pair.get("liquidity") or {}).get("usd")),
        "volume_5m": number(vol.get("m5")),
        "volume_1h": number(vol.get("h1")),
        "buys_5m": number((tx.get("m5") or {}).get("buys")),
        "sells_5m": number((tx.get("m5") or {}).get("sells")),
        "buys_1h": number((tx.get("h1") or {}).get("buys")),
        "sells_1h": number((tx.get("h1") or {}).get("sells")),
        "change_5m": number(changes.get("m5")),
        "change_1h": number(changes.get("h1")),
        "price": number(pair.get("priceUsd")),
    }

def evaluate(pair, strategy, age):
    f = features(pair, age)
    c = CONFIG[strategy]
    volume = f["volume_5m"] if strategy.endswith("new") else f["volume_1h"]
    buys = f["buys_5m"] if strategy.endswith("new") else f["buys_1h"]
    sells = f["sells_5m"] if strategy.endswith("new") else f["sells_1h"]
    change = f["change_5m"] if strategy.endswith("new") else f["change_1h"]
    reasons = []
    def check(ok, reason):
        if not ok:
            reasons.append(reason)
    check(f["age"] is not None and c["age"][0] <= f["age"] <= c["age"][1], "idade")
    check(f["cap"] is not None and c["cap"][0] <= f["cap"] <= c["cap"][1], "market cap")
    check(f["liq"] is not None and f["liq"] >= c["liq"], "liquidez")
    check(volume is not None and volume >= c["vol"], "volume")
    check(buys is not None and sells is not None and buys >= max(1, sells)*c["ratio"] and sells >= 3, "pressão compradora")
    check(change is not None and c["change"][0] <= change <= c["change"][1], "variação")
    check(f["price"] is not None and f["price"] > 0, "preço")
    # Pontuação de oportunidade, NÃO probabilidade de lucro ou auditoria de segurança.
    score = 0
    score += 20 if f["liq"] is not None and f["liq"] >= c["liq"]*1.5 else 10 if f["liq"] is not None and f["liq"] >= c["liq"] else 0
    score += 20 if volume is not None and volume >= c["vol"]*2 else 12 if volume is not None and volume >= c["vol"] else 0
    ratio = buys/max(sells,1) if buys is not None and sells is not None else 0
    score += 25 if 1.5 <= ratio <= 4 else 16 if c["ratio"] <= ratio <= 6 else 0
    score += 20 if change is not None and 0 <= change <= min(15,c["change"][1]) else 10 if change is not None and c["change"][0] <= change <= c["change"][1] else 0
    score += 15 if f["age"] is not None and c["age"][0] <= f["age"] <= c["age"][1] else 0
    min_score = MIN_SCORE_BY_STRATEGY.get(strategy, MIN_SCORE)
    return score, not reasons and score >= min_score, ", ".join(reasons) or (f"pontuação abaixo de {min_score}" if score < min_score else "aprovado"), f

def record(mint, strategy, score, eligible, reason, f):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?,?,?)",
                    (mint,strategy,int(time.time()),score,int(eligible),reason,json.dumps(f)))

def start_cohort(mint, strategy, f):
    if not f.get("price") or f["price"] <= 0:
        return
    now = int(time.time())
    with db() as con:
        # Uma coorte por contrato/estratégia em 12h, evitando excesso de consultas.
        last = con.execute("SELECT MAX(started) FROM cohorts WHERE mint=? AND strategy=?", (mint,strategy)).fetchone()[0]
        if last is None or now-last >= 43200:
            con.execute("INSERT INTO cohorts VALUES (?,?,?,?,?)", (mint,strategy,now,f["price"],json.dumps(f)))

def due(limit=8):
    now = int(time.time())
    with db() as con:
        rows = con.execute("SELECT mint,strategy,started,price FROM cohorts WHERE started>=? ORDER BY started DESC LIMIT 300",
                           (now-86400,)).fetchall()
        result = []
        for mint,strategy,started,price in rows:
            for horizon in WINDOWS:
                if started+horizon > now:
                    continue
                exists = con.execute("SELECT 1 FROM outcomes WHERE mint=? AND strategy=? AND started=? AND horizon=?",
                                     (mint,strategy,started,horizon)).fetchone()
                if not exists:
                    result.append((mint,strategy,started,price,horizon))
                if len(result) >= limit:
                    return result
        return result

def settle(mint,strategy,started,price,horizon,observed_price):
    current = number(observed_price)
    ret = (current/price-1)*100 if current is not None and current>0 and price>0 else None
    status = "observed" if ret is not None else "unavailable"
    with db() as con:
        con.execute("INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?,?,?,?)",
                    (mint,strategy,started,horizon,int(time.time()),ret,status))

def path_due(limit=4):
    """Return mature pattern cohorts needing one bounded candle-path query."""
    now = int(time.time())
    cutoff = max(WINDOWS)
    with db() as con:
        return con.execute("""SELECT c.mint,c.strategy,c.started,c.price
            FROM cohorts c
            LEFT JOIN path_jobs j
              ON j.mint=c.mint AND j.strategy=c.strategy AND j.started=c.started
            WHERE c.started>=? AND c.started+?<=?
              AND c.features LIKE '%"setup_type"%'
              AND COALESCE(j.attempts,0)<3
              AND COALESCE(j.status,'')!='observed'
            ORDER BY c.started ASC LIMIT ?""",
            (now-86400,cutoff,now,max(1,int(limit)))).fetchall()


def record_path_metrics(mint,strategy,started,entry_price,candles):
    """Store candle-based MFE/MAE and a clearly approximate net excursion."""
    entry = number(entry_price)
    valid = []
    source_candles = candles if isinstance(candles,list) else []
    for candle in source_candles:
        if not isinstance(candle,dict):
            continue
        stamp = number(candle.get("time"))
        high, low = number(candle.get("high")), number(candle.get("low"))
        if stamp is None or high is None or low is None or high<=0 or low<=0:
            continue
        if stamp > 100000000000:
            stamp /= 1000
        valid.append((stamp,high,low))

    with db() as con:
        prior = con.execute(
            "SELECT attempts FROM path_jobs WHERE mint=? AND strategy=? AND started=?",
            (mint,strategy,started)).fetchone()
        attempts = (prior[0] if prior else 0) + 1
        observed = bool(valid and entry and entry>0)
        job_status = "observed" if observed else "unavailable"
        con.execute("""INSERT OR REPLACE INTO path_jobs
            VALUES (?,?,?,?,?,?)""",
            (mint,strategy,started,attempts,job_status,int(time.time())))
        if not observed:
            return False

        row = con.execute(
            "SELECT features FROM cohorts WHERE mint=? AND strategy=? AND started=?",
            (mint,strategy,started)).fetchone()
        try:
            features = json.loads(row[0]) if row and row[0] else {}
        except (TypeError,ValueError):
            features = {}
        liquidity = number(features.get("liq"))
        test_size_usd = 50.0
        impact_pct = (
            test_size_usd / (liquidity / 2 + test_size_usd) * 100
            if liquidity and liquidity>0 else None
        )
        # GMGN's documented 1% transaction fee per side plus a
        # constant-product impact estimate for a fixed $50 paper order.
        cost_pct = 2 * (2.0 + impact_pct) if impact_pct is not None else None
        for horizon in WINDOWS:
            end = started + horizon
            path = [item for item in valid if started <= item[0] <= end]
            if not path:
                continue
            high = max(item[1] for item in path)
            low = min(item[2] for item in path)
            mfe = (high / entry - 1) * 100
            mae = (low / entry - 1) * 100
            net_mfe = mfe - cost_pct if cost_pct is not None else None
            net_mae = mae - cost_pct if cost_pct is not None else None
            con.execute("""INSERT OR REPLACE INTO path_metrics
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (mint,strategy,started,horizon,mfe,mae,net_mfe,net_mae,
                 cost_pct,len(path),"observed"))
    return True


def path_summary(horizon=1800):
    """Describe excursions by strategy and detected setup; never auto-tunes."""
    with db() as con:
        rows = con.execute("""SELECT c.strategy,c.features,p.mfe_pct,p.mae_pct,
                p.net_mfe_pct,p.net_mae_pct,p.estimated_cost_pct
            FROM path_metrics p
            JOIN cohorts c ON c.mint=p.mint AND c.strategy=p.strategy
                AND c.started=p.started
            WHERE p.horizon=? AND p.status='observed'""",(horizon,)).fetchall()
    grouped = {}
    for strategy,raw,mfe,mae,net_mfe,net_mae,cost in rows:
        try:
            features = json.loads(raw) if raw else {}
        except (TypeError,ValueError):
            features = {}
        setup = features.get("setup_type") or "setup não informado"
        key = f"{strategy}:{setup}"
        grouped.setdefault(key,[]).append((mfe,mae,net_mfe,net_mae,cost))
    result = {}
    for key,values in grouped.items():
        def mean_at(index):
            nums = [number(v[index]) for v in values if number(v[index]) is not None]
            return round(sum(nums)/len(nums),2) if nums else None
        result[key] = {
            "n":len(values),
            "mfe_bruto_medio_pct":mean_at(0),
            "mae_bruto_medio_pct":mean_at(1),
            "mfe_liquido_estimado_medio_pct":mean_at(2),
            "mae_liquido_estimado_medio_pct":mean_at(3),
            "custo_estimado_medio_pct":mean_at(4),
            "modelo":"ordem fictícia de US$50; taxa GMGN 1% por lado + impacto estimado; sem gás, MEV ou imposto do token",
            "validado":False,
        }
    return result


def summary(horizon=1800):
    # Diagnóstico descritivo, sem autoalteração dos pesos; evita alegar eficácia com amostra pequena.
    with db() as con:
        rows = con.execute("""SELECT c.strategy,o.return_pct FROM outcomes o
            JOIN cohorts c ON c.mint=o.mint AND c.strategy=o.strategy AND c.started=o.started
            WHERE o.horizon=? AND o.status='observed'""",(horizon,)).fetchall()
    result = {}
    for strategy in CONFIG:
        vals = [r for s,r in rows if s==strategy]
        result[strategy] = {"n":len(vals), "positive_rate":round(sum(x>0 for x in vals)/len(vals),3) if vals else None,
                            "mean_return_pct":round(sum(vals)/len(vals),2) if vals else None,
                            "validated":False}
    return result
