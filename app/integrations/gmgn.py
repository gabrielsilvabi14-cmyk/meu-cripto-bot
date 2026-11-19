"""Read-only GMGN Agent API integration, adapted from the existing Radar."""
import json
import os
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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
        with urlopen(req, timeout=20) as resp:
            result = json.load(resp)
    except HTTPError as exc:
        raise RuntimeError(f"GMGN HTTP {exc.code}") from None
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"GMGN indisponível ({type(exc).__name__})") from None
    if not isinstance(result, dict):
        raise RuntimeError("Resposta GMGN inesperada")
    if str(result.get("code", "0")) != "0":
        raise RuntimeError(f"GMGN código {str(result['code'])[:20]}")
    data = result.get("data", result)
    rows = data if isinstance(data, list) else next(
        (data[k] for k in ("rank", "list", "tokens", "items") if isinstance(data, dict) and isinstance(data.get(k), list)), [])
    output = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        address = item.get("address") or item.get("token_address")
        if not address:
            continue
        output.append({"chain": chain, "address": str(address)[:128],
                       "symbol": str(item.get("symbol") or "?")[:40],
                       "name": str(item.get("name") or "")[:100],
                       "price": item.get("price"), "liquidity": item.get("liquidity"),
                       "volume_1h": item.get("volume_1h") or item.get("volume"),
                       "source": "GMGN ranking; sem validação Pump.fun"})
    return output
