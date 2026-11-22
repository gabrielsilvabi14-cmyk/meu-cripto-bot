"""Read-only GMGN Agent API integration, adapted from the existing Radar."""
import json
import logging
import os
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gmgn_shared_limit import request_slot, note_rate_limit

HOST = "https://openapi.gmgn.ai"

def fetch_rank(chain: str, interval: str = "5m", limit: int = 20) -> list[dict]:
    if chain not in ("sol", "eth") or interval not in ("5m", "1h"):
        raise ValueError("Unsupported market query")
    key = os.environ.get("GMGN_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GMGN_API_KEY não configurada")
    query = urlencode({"chain": chain, "interval": interval, "order_by": "volume",
                       "limit": min(limit, 100), "timestamp": int(time.time()),
                       "client_id": str(uuid.uuid4())})
    req = Request(f"{HOST}/v1/market/rank?{query}",
                  headers={"X-APIKEY": key, "User-Agent": "gmgn-cli/1.7.1",
                           "Accept": "application/json"})
    try:
        with request_slot():
            with urlopen(req, timeout=20) as resp:
                result = json.load(resp)
    except HTTPError as exc:
        if exc.code == 429:
            note_rate_limit(exc.headers)
        raise RuntimeError(f"GMGN HTTP {exc.code}") from None
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"GMGN indisponível ({type(exc).__name__})") from None
    if not isinstance(result, dict):
        raise RuntimeError("Resposta GMGN inesperada")
    if str(result.get("code", "0")) != "0":
        raise RuntimeError(f"GMGN código {str(result['code'])[:20]}")
    data = result.get("data", result)
    def rows_from(node, depth=0):
        if depth > 4:
            return []
        if isinstance(node, list):
            if any(isinstance(x, dict) and (x.get("address") or x.get("token_address")) for x in node):
                return node
            for item in node:
                found = rows_from(item, depth + 1)
                if found:
                    return found
        if isinstance(node, dict):
            for key in ("rank", "list", "tokens", "items", "data", "rows", "result"):
                if key in node:
                    found = rows_from(node[key], depth + 1)
                    if found:
                        return found
        return []
    rows = rows_from(data)
    if not rows:
        raise RuntimeError("GMGN respondeu, mas ranking veio vazio ou com formato não reconhecido")
    def metric(item, *keys):
        for k in keys:
            v = item.get(k)
            if isinstance(v, dict):
                v = v.get("usd") or v.get("value")
            try:
                if v is not None and v != "":
                    return float(v)
            except (ValueError, TypeError):
                pass
        return None
    output = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        address = item.get("address") or item.get("token_address")
        if not address:
            continue
        created = metric(item, "created_timestamp", "creation_timestamp", "open_timestamp")
        if created and created > 1e11:
            created /= 1000
        age = (time.time() - created) / 3600 if created and created <= time.time() else None
        output.append({"chain": chain, "address": str(address)[:128],
                       "symbol": str(item.get("symbol") or "?")[:40],
                       "name": str(item.get("name") or "")[:100],
                       "price": item.get("price") or item.get("price_usd"), "liquidity": item.get("liquidity"),
                       "volume_1h": item.get("volume_1h") or item.get("volume"),
                       "buys_1h": metric(item, "buys_1h", "buys"),
                       "sells_1h": metric(item, "sells_1h", "sells"),
                       "change_1h": metric(item, "price_change_percent1h", "price_change_percent", "change_1h"),
                       "age_hours": age, "market_cap": metric(item, "market_cap", "usd_market_cap", "marketCap"),
                       "source": "GMGN ranking; sem validação Pump.fun"})
    return output


_GMGN_INFO_BACKOFF_UNTIL = 0.0
_GMGN_INFO_WARNED = False

def _number(value):
    try:
        result = float(value)
        return result if 0 < result < 1e12 and result == result else None
    except (TypeError, ValueError, OverflowError):
        return None

def fetch_token_snapshot(chain: str, address: str) -> dict | None:
    """Read-only Agent API token info; back off on auth errors to avoid hammering."""
    global _GMGN_INFO_BACKOFF_UNTIL, _GMGN_INFO_WARNED
    if chain not in ("sol", "eth") or not address or time.monotonic() < _GMGN_INFO_BACKOFF_UNTIL:
        return None
    key = os.getenv("GMGN_API_KEY", "").strip()
    if not key:
        return None
    req = Request(f"{HOST}/v1/token/info?" + urlencode({"chain": chain, "address": address, "timestamp": int(time.time()), "client_id": str(uuid.uuid4())}),
                  headers={"X-APIKEY": key, "User-Agent": "gmgn-cli/1.7.1", "Accept": "application/json"})
    try:
        with request_slot():
            with urlopen(req, timeout=8) as resp:
                payload = json.load(resp)
        if not isinstance(payload, dict) or str(payload.get("code", "0")) != "0":
            logging.warning("GMGN token info returned non-success code")
            return None
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            return None
        for candidate in (data, data.get("token"), data.get("token_info"), data.get("info")):
            if not isinstance(candidate, dict):
                continue
            price_obj = candidate.get("price")
            price = _number(price_obj.get("price")) if isinstance(price_obj, dict) else _number(price_obj)
            if price is None:
                price = _number(candidate.get("price_usd") or candidate.get("priceUsd"))
            if price is None:
                continue
            stat = candidate.get("stat") if isinstance(candidate.get("stat"), dict) else {}
            price_data = price_obj if isinstance(price_obj, dict) else {}
            def ratio(field):
                v = stat.get(field)
                try:
                    f = float(v)
                    return f if 0 <= f <= 1 else None
                except (ValueError, TypeError, OverflowError):
                    return None
            def audit_ratio(*names):
                for node in (stat, candidate, candidate.get("security"), candidate.get("risk")):
                    if not isinstance(node, dict):
                        continue
                    for name in names:
                        value = node.get(name)
                        try:
                            number = float(value)
                        except (TypeError, ValueError, OverflowError):
                            continue
                        # GMGN rate fields are 0..1; percent fields explicitly 0..100.
                        if name.endswith(("_percent", "_percentage")) and number > 1:
                            number /= 100
                        if 0 <= number <= 1:
                            return number
                return None
            result = {"price": price, "source": "gmgn",
                      "liquidity": _number(candidate.get("liquidity")),
                      "buy_volume_1m": _number(price_data.get("buy_volume_1m")),
                      "sell_volume_1m": _number(price_data.get("sell_volume_1m")),
                      "buys_1m": _number(price_data.get("buys_1m")),
                      "sells_1m": _number(price_data.get("sells_1m")),
                      "top10_ratio": audit_ratio("top_10_holder_rate", "top10_holder_rate", "top10_holder_percent"),
                      "sniper_ratio": audit_ratio("top70_sniper_hold_rate", "sniper_rate"),
                      "bundler_ratio": audit_ratio("top_bundler_trader_percentage", "bundler_rate", "bundler_trader_amount_rate"),
                      "insider_ratio": ratio("top_rat_trader_percentage"),
                      "dev_hold_ratio": ratio("dev_team_hold_rate"),
                      "launchpad_status": candidate.get("launchpad_status")}
            _GMGN_INFO_WARNED = False
            logging.info("PAPER PRICE source=gmgn %s:%s price_usd=%.12g", chain, chain, address[:8], price)
            return result
    except HTTPError as exc:
        if exc.code == 429:
            note_rate_limit(exc.headers)
            _GMGN_INFO_BACKOFF_UNTIL = time.monotonic() + 120
        elif exc.code in (401, 403):
            _GMGN_INFO_BACKOFF_UNTIL = time.monotonic() + 600
        if not _GMGN_INFO_WARNED:
            logging.warning("GMGN token/info HTTP %s; fallback DexScreener, retry after cooldown (verify Agent API auth/permissions)", exc.code)
            _GMGN_INFO_WARNED = True
    except (URLError, TimeoutError, ValueError, TypeError) as exc:
        logging.warning("GMGN token/info %s; fallback", type(exc).__name__)
    return None

def _gmgn_spot_price(chain: str, address: str) -> float | None:
    snapshot = fetch_token_snapshot(chain, address)
    return snapshot["price"] if snapshot else None


def _dexscreener_spot(chain: str, address: str) -> dict | None:
    if chain not in ("sol", "eth"):
        return None
    try:
        req = Request("https://api.dexscreener.com/latest/dex/tokens/" + address,
                      headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=8) as resp:
            result = json.load(resp)
        pairs = [p for p in (result.get("pairs") or [])
                 if isinstance(p, dict) and p.get("chainId") == ("ethereum" if chain == "eth" else "solana")
                 and str((p.get("baseToken") or {}).get("address", "")).lower() == address.lower()]
        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
        for pair in pairs:
            price = _number(pair.get("priceUsd"))
            if price is not None:
                liquidity = _number((pair.get("liquidity") or {}).get("usd"))
                logging.info("PAPER PRICE source=dexscreener %s:%s price_usd=%.12g liquidity_usd=%s", chain, address[:8], price, liquidity)
                return {"price": price, "source": "dexscreener", "liquidity": liquidity}
    except HTTPError as exc:
        logging.warning("DexScreener HTTP %s for %s:%s", exc.code, chain, address[:8])
    except (URLError, TimeoutError, ValueError, TypeError) as exc:
        logging.warning("DexScreener %s for %s:%s", type(exc).__name__, chain, address[:8])
    return None


def _geckoterminal_spot(chain: str, address: str) -> dict | None:
    """Independent Solana spot quote from GeckoTerminal's public token endpoint."""
    if chain not in ("sol", "eth"):
        return None
    try:
        req = Request("https://api.geckoterminal.com/api/v2/networks/" + ("eth" if chain == "eth" else "solana") + "/tokens/" + address,
                      headers={"Accept": "application/json;version=20230302", "User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=8) as resp:
            payload = json.load(resp)
        token = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(token, dict):
            return None
        attributes = token.get("attributes") or {}
        price = _number(attributes.get("price_usd"))
        if price is not None:
            logging.info("PAPER PRICE source=geckoterminal %s:%s price_usd=%.12g", chain, address[:8], price)
            return {"price": price, "source": "geckoterminal"}
    except HTTPError as exc:
        logging.warning("GeckoTerminal HTTP %s for sol:%s", exc.code, address[:8])
    except (URLError, TimeoutError, ValueError, TypeError) as exc:
        logging.warning("GeckoTerminal %s for sol:%s", type(exc).__name__, address[:8])
    return None


def fetch_token_price(chain: str, address: str) -> float | None:
    observation = fetch_market_observation(chain, address)
    return observation["price"] if observation else None


def fetch_token_market_cap(chain: str, address: str) -> float | None:
    """Get reported market cap, never substitute FDV for circulating market cap."""
    if chain not in ("sol","eth") or not address:
        return None
    key=os.environ.get("GMGN_API_KEY","").strip()
    if not key:
        return None
    req=Request(f"{HOST}/v1/token/info?"+urlencode({"chain":chain,"address":address,"timestamp":int(time.time()),"client_id":str(uuid.uuid4())}),headers={"X-APIKEY":key,"User-Agent":"gmgn-cli/1.7.1","Accept":"application/json"})
    try:
        with request_slot():
            with urlopen(req,timeout=15) as resp:
                result=json.load(resp)
        if str(result.get("code","0"))!="0":
            return None
        data=result.get("data",result)
        if not isinstance(data,dict):
            return None
        for node in (data,data.get("token"),data.get("token_info"),data.get("info")):
            if isinstance(node,dict):
                for field in ("market_cap","usd_market_cap","marketCap"):
                    try:
                        value=float(node.get(field))
                        if 0<value<1e15:
                            return value
                    except (TypeError,ValueError):
                        pass
    except (HTTPError,URLError,TimeoutError,ValueError,TypeError) as exc:
        if isinstance(exc, HTTPError) and exc.code == 429:
            note_rate_limit(exc.headers)
    return None

def fetch_market_observation(chain: str, address: str) -> dict | None:
    """GMGN Agent API first, DexScreener second, GeckoTerminal third."""
    if chain not in ("sol", "eth") or not address:
        return None
    for source in (fetch_token_snapshot, _dexscreener_spot, _geckoterminal_spot):
        result = source(chain, address)
        if result is not None and _number(result.get("price")) is not None:
            return result
    logging.error("PAPER PRICE ALL SOURCES FAILED %s:%s; no synthetic exit price", chain, address[:8])
    return None
