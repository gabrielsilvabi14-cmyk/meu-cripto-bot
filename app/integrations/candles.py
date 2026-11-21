"""Read-only confirmed OHLCV candles from GeckoTerminal; no synthetic candles."""
import json
import logging
import time
import random
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

_CACHE = {}
BASE = "https://api.geckoterminal.com/api/v2/networks/solana"
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "Mozilla/5.0"}

def _get(url):
    req = Request(url, headers=HEADERS)
    for attempt in range(3):
        try:
            with urlopen(req, timeout=7) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            try:
                delay = min(8.0, max(0.5, float(exc.headers.get("Retry-After"))))
            except (ValueError, TypeError):
                delay = 1.5 * (2 ** attempt)
            logging.warning("OHLCV HTTP %s retry %s/2", exc.code, attempt + 1)
            time.sleep(delay + random.uniform(0, 0.3))
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(1.5 * (2 ** attempt))

def fetch_minute_candles(address, limit=40):
    """Returns [] on missing/invalid data; only completed minute candles."""
    if not isinstance(address, str) or not address or not all(c.isalnum() for c in address):
        return []
    now = time.monotonic()
    cached = _CACHE.get(address)
    if cached and now - cached[0] < (60 if cached[1] else 15):
        return cached[1]
    candles = []
    failed = False
    try:
        data = _get(BASE + "/tokens/" + address + "/pools?page=1")
        pools = data.get("data", [])
        pool = next((x for x in pools if isinstance(x, dict) and x.get("id")), None)
        if pool:
            pool_id = pool["id"].split("_", 1)[-1]
            from urllib.parse import quote
            url = BASE + "/pools/" + quote(pool_id, safe="") + "/ohlcv/minute?aggregate=1&limit=" + str(min(max(int(limit), 5), 100))
            raw = _get(url).get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            current_minute = int(time.time() // 60) * 60
            for row in raw:
                if not isinstance(row, list) or len(row) < 6:
                    continue
                try:
                    ts = int(row[0])
                    o, h, l, c, v = (float(x) for x in row[1:6])
                except (TypeError, ValueError, OverflowError):
                    continue
                if ts >= current_minute or min(o, h, l, c) <= 0 or v < 0 or h < max(o, c) or l > min(o, c):
                    continue
                candles.append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
            candles.sort(key=lambda x: x["ts"])
            candles = candles[-limit:]
    except (HTTPError, URLError, TimeoutError, ValueError, TypeError, KeyError) as exc:
        failed = True
        logging.warning("OHLCV unavailable token=%s error=%s status=%s", address[:8], type(exc).__name__, getattr(exc, "code", "n/a"))
    if not failed:
        _CACHE[address] = (time.monotonic(), candles)
    if len(_CACHE) > 400:
        _CACHE.clear()
    return candles

def classify(candles):
    """Price-volume patterns with explicit minimum history and no prediction claims."""
    if len(candles) < 8:
        return []
    recent = candles[-8:]
    # Require consecutive minute bars; no filling gaps.
    if any(b["ts"] - a["ts"] != 60 for a, b in zip(recent, recent[1:])):
        return []
    closes = [x["close"] for x in recent]
    vols = [x["volume"] for x in recent]
    last = recent[-1]
    patterns = []
    if all(closes[i] >= closes[i-1] * 0.97 for i in range(1, 8)) and closes[-1] > closes[0] * 1.06 and sum(vols[-3:]) > sum(vols[:3]) * 1.5:
        patterns.append("acumulacao_gradual_volume_crescente")
    if last["close"] > max(x["high"] for x in recent[:-1]) * 1.02 and last["volume"] > sum(vols[:-1]) / 7 * 1.7:
        patterns.append("rompimento_com_volume")
    if closes[-1] < max(x["high"] for x in recent) * 0.75:
        patterns.append("queda_25pct_do_pico")
    if closes[-1] > closes[-2] * 1.06 and min(x["low"] for x in recent[:-1]) < closes[0] * 0.80:
        patterns.append("reversao_apos_queda")
    if last["close"] < last["open"] * 0.85 and last["volume"] > sum(vols[:-1]) / 7 * 2:
        patterns.append("venda_forte_volume_elevado")
    return patterns
