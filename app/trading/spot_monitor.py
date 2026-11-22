"""Observed spot-price monitor; never fabricates OHLCV or triggers orders."""
from collections import deque
from datetime import datetime, timezone
from math import isfinite
from threading import RLock

_LOCK = RLock()
_HISTORY = {}
_MAX_TOKENS = 60
_MAX_SAMPLES = 30

def record(chain, address, price, volume_1h=None, timestamp=None):
    """One genuine price observation per UTC minute per token."""
    if chain not in ("sol", "eth") or not address:
        return None
    try:
        price = float(price)
        if not isfinite(price) or price <= 0:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    minute = int((timestamp or datetime.now(timezone.utc)).timestamp() // 60)
    key = chain + ":" + address
    with _LOCK:
        if key not in _HISTORY and len(_HISTORY) >= _MAX_TOKENS:
            oldest = min(_HISTORY, key=lambda k: _HISTORY[k][-1]["minute"])
            del _HISTORY[oldest]
        samples = _HISTORY.setdefault(key, deque(maxlen=_MAX_SAMPLES))
        if samples and minute < samples[-1]["minute"]:
            return None
        sample = {"minute": minute, "price": price}
        try:
            volume = float(volume_1h)
            if isfinite(volume) and volume >= 0:
                sample["volume_1h"] = volume
        except (TypeError, ValueError, OverflowError):
            pass
        if samples and minute == samples[-1]["minute"]:
            samples[-1] = sample
        else:
            samples.append(sample)
        return analyze(chain, address)

def analyze(chain, address):
    """Returns descriptive observed changes, not candle signals or buy advice."""
    with _LOCK:
        data = list(_HISTORY.get(chain + ":" + address, ()))
    if not data:
        return {"status": "no_samples"}
    latest = data[-1]
    result = {"status": "collecting", "samples": len(data), "last_minute": latest["minute"], "source": "observed_spot"}
    for window in (5, 10, 15):
        prior = next((s for s in reversed(data[:-1]) if s["minute"] <= latest["minute"] - window), None)
        if prior and latest["minute"] - prior["minute"] <= window + 2:
            result["change_" + str(window) + "m_pct"] = round((latest["price"] / prior["price"] - 1) * 100, 3)
            if "volume_1h" in latest and "volume_1h" in prior:
                result["volume_1h_delta_" + str(window) + "m"] = round(latest["volume_1h"] - prior["volume_1h"], 2)
    if any(k.startswith("change_") for k in result):
        result["status"] = "observed"
    return result
