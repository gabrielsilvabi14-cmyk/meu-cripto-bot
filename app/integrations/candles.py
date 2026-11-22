"""Read-only confirmed OHLCV candles from GeckoTerminal; no synthetic candles."""
import json
import logging
import time
import random
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

_CACHE = {}
_POOL_CACHE = {}
_LOCK = threading.RLock()
_NEXT_REQUEST = 0.0
_COOLDOWN_UNTIL = 0.0
_RATE_LIMIT_STREAK = 0
_LAST_429_AT = 0.0
_METRICS = {"requests": 0, "http_429": 0, "cache_hits": 0, "successful_candles": 0}

class RateLimited(Exception):
    pass
BASE = "https://api.geckoterminal.com/api/v2/networks/solana"
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "Mozilla/5.0"}

def _get(url):
    global _NEXT_REQUEST, _COOLDOWN_UNTIL, _RATE_LIMIT_STREAK, _LAST_429_AT
    with _LOCK:
        now = time.monotonic()
        if now < _COOLDOWN_UNTIL:
            raise RateLimited("global OHLCV cooldown")
        wait = max(0.0, _NEXT_REQUEST - now)
        if wait:
            time.sleep(wait)
        _NEXT_REQUEST = time.monotonic() + 12.0
        _METRICS["requests"] += 1
    try:
        with urlopen(Request(url, headers=HEADERS), timeout=7) as response:
            payload = json.load(response)
        with _LOCK:
            if time.monotonic() - _LAST_429_AT > 1800:
                _RATE_LIMIT_STREAK = 0
        return payload
    except HTTPError as exc:
        if exc.code == 429:
            try:
                delay = min(300.0, max(60.0, float(exc.headers.get("Retry-After", "60"))))
            except (ValueError, TypeError):
                delay = 60.0
            with _LOCK:
                current = time.monotonic()
                _RATE_LIMIT_STREAK = _RATE_LIMIT_STREAK + 1 if current - _LAST_429_AT < 1800 else 1
                _LAST_429_AT = current
                delay = max(delay, min(900.0, 60.0 * (2 ** min(_RATE_LIMIT_STREAK - 1, 4))))
                _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, current + delay)
            _METRICS["http_429"] += 1
            logging.warning("OHLCV HTTP 429: cooldown %.0fs streak=%s", delay, _RATE_LIMIT_STREAK)
            raise RateLimited("HTTP 429") from exc
        raise

def fetch_minute_candles(address, limit=40, chain="sol"):
    """Returns [] on missing/invalid data; only completed minute candles."""
    if chain not in ("sol", "eth") or not isinstance(address, str) or not address or not all(c.isalnum() for c in address):
        return []
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get((chain, address))
    if cached and now - cached[0] < (75 if cached[1] else 180):
        with _LOCK:
            _METRICS["cache_hits"] += 1
        return cached[1]
    candles = []
    failed = False
    try:
        with _LOCK:
            pc = _POOL_CACHE.get((chain, address))
        if pc and time.monotonic() - pc[0] < 900:
            pool_id = pc[1]
        else:
            data = _get(BASE.replace("/solana", "/eth" if chain == "eth" else "/solana") + "/tokens/" + address + "/pools?page=1")
            pools = [p for p in data.get("data", []) if isinstance(p, dict) and p.get("id")]
            def pool_rank(p):
                attrs = p.get("attributes") or {}
                try:
                    return float(attrs.get("reserve_in_usd") or 0)
                except (ValueError, TypeError):
                    return 0.0
            pools.sort(key=pool_rank, reverse=True)
            pool_id = pools[0]["id"].split("_", 1)[-1] if pools else None
            if not pool_id:
                logging.info("OHLCV no pool token=%s", address[:8])
            if pool_id:
                with _LOCK:
                    _POOL_CACHE[(chain, address)] = (time.monotonic(), pool_id)
        if pool_id:
            from urllib.parse import quote
            url = BASE.replace("/solana", "/eth" if chain == "eth" else "/solana") + "/pools/" + quote(pool_id, safe="") + "/ohlcv/minute?aggregate=1&token=" + quote(address, safe="") + "&limit=" + str(min(max(int(limit), 5), 100))
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
            if candles and current_minute - candles[-1]["ts"] > 180:
                logging.warning("OHLCV stale token=%s last_candle_age_s=%s", address[:8], current_minute - candles[-1]["ts"])
                candles = []
    except (HTTPError, URLError, TimeoutError, ValueError, TypeError, KeyError, RateLimited) as exc:
        failed = True
        logging.warning("OHLCV unavailable token=%s error=%s status=%s", address[:8], type(exc).__name__, getattr(exc, "code", "n/a"))
    with _LOCK:
        if not failed:
            _CACHE[(chain, address)] = (time.monotonic(), candles)
            if candles:
                _METRICS["successful_candles"] += 1
        if len(_CACHE) > 400:
            _CACHE.clear()
        if len(_POOL_CACHE) > 400:
            _POOL_CACHE.clear()
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


def ohlcv_metrics():
    """Thread-safe diagnostic counters; excludes API credentials."""
    with _LOCK:
        return dict(_METRICS, cooldown_seconds=max(0, round(_COOLDOWN_UNTIL-time.monotonic())))
