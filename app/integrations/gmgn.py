"""Read-only GMGN Agent API integration, adapted from the existing Radar."""
import json
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
                       "limit": min(limit, 30), "timestamp": int(time.time()),
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


def fetch_token_price(chain: str, address: str) -> float | None:
    """Refresh an open paper position even when it is absent from ranking."""
    if chain not in ("sol","eth") or not address:
        return None
    key=os.environ.get("GMGN_API_KEY","").strip()
    if not key:
        return None
    query=urlencode({"chain":chain,"address":address})
    req=Request(f"{HOST}/v1/token/info?{query}",headers={"X-APIKEY":key,"User-Agent":"gmgn-cli/1.7.1","Accept":"application/json"})
    try:
        with request_slot():
            with urlopen(req,timeout=15) as resp:
                result=json.load(resp)
        if str(result.get("code","0"))!="0":
            return None
        data=result.get("data",result)
        for node in (data,data.get("token") if isinstance(data,dict) else None,data.get("token_info") if isinstance(data,dict) else None,data.get("info") if isinstance(data,dict) else None):
            if isinstance(node,dict):
                for field in ("price","price_usd","priceUsd"):
                    try:
                        value=float(node.get(field))
                        if 0<value<1e12:
                            return value
                    except (TypeError,ValueError):
                        pass
    except (HTTPError,URLError,TimeoutError,ValueError,TypeError) as exc:
        import logging
        if isinstance(exc, HTTPError) and exc.code == 429:
            note_rate_limit(exc.headers)
        logging.warning("Position price unavailable for %s:%s (%s)",chain,address[:8],type(exc).__name__)
    return None

def fetch_token_market_cap(chain: str, address: str) -> float | None:
    """Get reported market cap, never substitute FDV for circulating market cap."""
    if chain not in ("sol","eth") or not address:
        return None
    key=os.environ.get("GMGN_API_KEY","").strip()
    if not key:
        return None
    req=Request(f"{HOST}/v1/token/info?"+urlencode({"chain":chain,"address":address}),headers={"X-APIKEY":key,"User-Agent":"gmgn-cli/1.7.1","Accept":"application/json"})
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
