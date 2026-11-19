import os
import json
import time
import html
import logging
import math
import threading
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo
import influencer_radar
import radar_intelligence as intelligence
import whale_risk
import paper_trading
import viral_events

# ============================================================
# RADAR MULTICHAIN 4.10
# Descoberta + acompanhamento simulado
# GMGN Agent API | Telegram
# Somente leitura; sem operações automáticas
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GMGN_API_KEY = os.getenv("GMGN_API_KEY", "").strip()
GMGN_API_HOST = "https://openapi.gmgn.ai"
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PORT = int(os.getenv("PORT", "10000"))

INTERVALO = max(60, int(os.getenv("RADAR_INTERVAL", "60")))
INTERVALO_RADARES = 15
INTERVALO_ACOMPANHAMENTO = 15
FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")
VERSAO = "4.10.1"

DEDUP_SEGUNDOS = 12 * 3600
JANELA_SAIDA_NEW = 30 * 60
JANELAS_PROMOVIDOS = tuple(range(3 * 3600, 24 * 3600 + 1, 3 * 3600))
TOLERANCIA_SAIDA_NEW = 2 * 60
TOLERANCIA_PROMOVIDOS = 5 * 60
MAX_CANDIDATOS = 30
MAX_ALERTAS = 3

LIQ_MIN = 20000
VOL_NEW_MIN = 30000
VOL_TREND_MIN = 30000
BUYS_MIN = 30
SELLS_MIN = 10
BUY_RATIO_MIN = 1.15
CHANGE_MIN = -15
CHANGE_MAX = 80
IDADE_NEW_MIN = 10 / 60
IDADE_NEW_MAX = 3
NEW_CHANGE_MIN = 3
NEW_CHANGE_MAX = 65
NEW_BUY_SHARE_MAX = 0.85
NEW_TICKET_MIN = 15
NEW_VOLUME_LIQ_MAX = 20
NEW_TXNS_5M_MIN = 12
NEW_SELLS_5M_MIN = 4
NEW_CHANGE_5M_MAX = 25

STATE_FILE = Path(
    os.getenv("RADAR_STATE_FILE", "radar_state.json")
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("radar")

lock = threading.RLock()
state = {"seen": {}, "records": {}, "influencer_posts": {}}
gmgn_block_until = 0.0
gmgn_last_request = 0.0
gmgn_next_request_at = 0.0
gmgn_request_lock = threading.RLock()
gmgn_info_cache = {}
# Intervalo conservador para reduzir HTTP 429 na Agent API.
GMGN_RATE_WEIGHT_PER_SECOND = 0.5
GMGN_CACHE_TTL = 180


# ============================================================
# SERVIDOR HTTP
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            return

        body = json.dumps({
            "status": "online",
            "version": VERSAO,
            "time": datetime.now(timezone.utc).isoformat()
        }).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_server():
    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT), HealthHandler
    )
    log.info("HTTP ativo na porta %s", PORT)
    server.serve_forever()


# ============================================================
# HTTP E TELEGRAM
# ============================================================

def request_json(url, data=None, timeout=20):
    """HTTP JSON para Telegram; não consulta fontes de mercado externas."""
    headers = {
        "User-Agent": f"SolanaRadar/{VERSAO}",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = Request(
        url, data=data, headers=headers,
        method="POST" if data is not None else "GET"
    )
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def retry_delay(headers):
    """Calcula espera do HTTP 429, limitada entre 1s e 15 minutos."""
    delay = 60.0
    if headers:
        raw = headers.get("Retry-After")
        if raw:
            try:
                delay = max(1.0, float(raw))
            except (TypeError, ValueError):
                from email.utils import parsedate_to_datetime
                try:
                    delay = max(1.0, parsedate_to_datetime(raw).timestamp() - time.time())
                except (TypeError, ValueError, OverflowError):
                    pass
        reset = headers.get("X-RateLimit-Reset")
        if reset:
            try:
                epoch = float(reset)
                if epoch > time.time():
                    delay = max(delay, epoch - time.time())
            except (TypeError, ValueError):
                pass
    return min(900.0, max(1.0, delay))


from gmgn_shared_limit import request_slot, note_rate_limit

def gmgn_request(path, method="GET", params=None, payload=None):
    """Serializa consultas da Agent API para compartilhar um único limitador."""
    with gmgn_request_lock:
        return _gmgn_request_serialized(path, method, params, payload)


def _gmgn_request_serialized(path, method="GET", params=None, payload=None):
    global gmgn_block_until, gmgn_last_request, gmgn_next_request_at

    if not GMGN_API_KEY:
        raise RuntimeError("GMGN_API_KEY não configurada no ambiente")

    remaining = gmgn_block_until - time.monotonic()
    if remaining > 0:
        raise RuntimeError(
            f"GMGN em pausa após limite de requisições por "
            f"{remaining:.0f}s"
        )

    weight = 3 if path in ("/v1/market/rank", "/v1/market/hot_searches") else 2 if path in ("/v1/trenches", "/v1/market/token_kline") else 5 if path in ("/v1/market/token_top_holders", "/v1/market/token_top_traders") else 1
    wait = gmgn_next_request_at - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    gmgn_last_request = time.monotonic()
    gmgn_next_request_at = gmgn_last_request + weight / GMGN_RATE_WEIGHT_PER_SECOND

    query = dict(params or {})
    query["timestamp"] = int(time.time())
    query["client_id"] = str(uuid.uuid4())
    url = f"{GMGN_API_HOST}{path}?{urlencode(query, doseq=True)}"
    body = None if payload is None else json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    headers = {
        "X-APIKEY": GMGN_API_KEY,
        "User-Agent": "gmgn-cli/1.7.1",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    req = Request(
        url, data=body, headers=headers,
        method=method.upper()
    )

    try:
        with request_slot():
            with urlopen(req, timeout=25) as response:
                result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", "replace")
        retry_at = None
        try:
            error_json = json.loads(error_body)
            retry_at = num(error_json.get("reset_at"))
        except (ValueError, AttributeError):
            pass
        if exc.code == 429:
            retry_after = max(retry_delay(exc.headers), note_rate_limit(exc.headers))
            if retry_at and retry_at > time.time():
                retry_after = max(retry_after, retry_at - time.time())
            retry_after = min(900, retry_after)
            gmgn_block_until = time.monotonic() + retry_after
            log.warning("GMGN HTTP 429 em %s; pausa %.0fs", path, retry_after)
            raise RuntimeError(f"GMGN HTTP 429; pausa {retry_after:.0f}s") from None
        raise RuntimeError(f"GMGN HTTP {exc.code} em {path}")
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Falha de conexão com GMGN: {type(exc).__name__}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError("GMGN retornou uma resposta inválida")

    code = result.get("code") if isinstance(result, dict) else None
    if code is not None and str(code) != "0":
        if str(code) == "429":
            reset_at = num(result.get("reset_at"))
            wait = max(1, reset_at - time.time()) if reset_at else 60
            gmgn_block_until = time.monotonic() + wait
            raise RuntimeError(
                f"GMGN limitou as consultas; nova tentativa em "
                f"aproximadamente {wait:.0f}s"
            )
        message = (
            result.get("message") or result.get("error")
            or f"código {code}"
        )
        raise RuntimeError(f"GMGN API: {message}")

    return result.get("data", result) if isinstance(result, dict) else result


def gmgn_token_info(mint, chain="sol"):
    cache_key = f"{chain}:{mint}"
    cached = gmgn_info_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < GMGN_CACHE_TTL:
        return cached[1]
    data = gmgn_request(
        "/v1/token/info", params={"chain": chain, "address": mint}
    )
    if isinstance(data, dict):
        for key in ("token", "token_info", "info"):
            if isinstance(data.get(key), dict):
                info = data[key]
                gmgn_info_cache[cache_key] = (time.monotonic(), info)
                return info
    info = data if isinstance(data, dict) else {}
    if len(gmgn_info_cache) > 300:
        gmgn_info_cache.clear()
    gmgn_info_cache[cache_key] = (time.monotonic(), info)
    return info


def gmgn_rows(node):
    rows = []
    if isinstance(node, dict):
        if node.get("address") and (
            node.get("symbol") or node.get("name")
        ):
            rows.append(node)
        else:
            for value in node.values():
                rows.extend(gmgn_rows(value))
    elif isinstance(node, list):
        for value in node:
            rows.extend(gmgn_rows(value))
    return rows


def gmgn_to_pair(row, category, info=None, chain="sol"):
    row = row if isinstance(row, dict) else {}
    info = info if isinstance(info, dict) else {}
    price_data = info.get("price") or {}
    if not isinstance(price_data, dict):
        price_data = {}

    mint = (
        row.get("address") or info.get("address")
        or row.get("token_address")
    )
    if not mint:
        return None

    price = num(price_data.get("price"))
    if price <= 0:
        price = num(row.get("price") or row.get("priceUsd"))

    supply = num(
        info.get("circulating_supply") or info.get("total_supply")
        or row.get("total_supply")
    )
    market_cap = num(
        row.get("market_cap") or row.get("usd_market_cap")
        or row.get("marketCap")
    )
    if market_cap <= 0 and price > 0 and supply > 0:
        market_cap = price * supply

    liquidity = num(
        row.get("liquidity") or info.get("liquidity")
        or (info.get("pool") or {}).get("liquidity")
    )
    volume_1h = num(
        row.get("volume") or row.get("volume_1h")
        or price_data.get("volume_1h")
    )
    buys_1h = num(
        row.get("buys") or row.get("buys_1h")
        or price_data.get("buys_1h")
    )
    sells_1h = num(
        row.get("sells") or row.get("sells_1h")
        or price_data.get("sells_1h")
    )

    change_1h = row.get("price_change_percent1h")
    if change_1h is None:
        change_1h = row.get("price_change_percent")
    change_1h = num(change_1h, default=None)
    start_1h = num(price_data.get("price_1h"))
    if change_1h is None and price > 0 and start_1h > 0:
        change_1h = (price / start_1h - 1) * 100

    start_5m = num(price_data.get("price_5m"))
    change_5m = (
        (price / start_5m - 1) * 100
        if price > 0 and start_5m > 0 else None
    )
    created = num(
        info.get("creation_timestamp")
        or row.get("created_timestamp")
        or row.get("creation_timestamp")
        or row.get("open_timestamp")
    )
    if created > 0:
        if created > 100000000000:
            created /= 1000
        created_ms = created * 1000
    else:
        created_ms = 0

    merged = dict(row)
    merged.update(info)
    return {
        "chainId": {"sol": "solana", "eth": "ethereum", "bsc": "bsc"}.get(chain, chain),
        "baseToken": {
            "address": mint,
            "name": info.get("name") or row.get("name") or "Token",
            "symbol": info.get("symbol") or row.get("symbol") or "???",
        },
        "pairAddress": mint,
        "priceUsd": price,
        "marketCap": market_cap,
        "liquidity": {"usd": liquidity},
        "volume": {"h1": volume_1h, "m5": num(price_data.get("volume_5m"))},
        "txns": {
            "h1": {"buys": buys_1h, "sells": sells_1h},
            "m5": {
                "buys": num(price_data.get("buys_5m")),
                "sells": num(price_data.get("sells_5m")),
            },
        },
        "priceChange": {"h1": change_1h, "m5": change_5m},
        "pairCreatedAt": created_ms,
        "gmgn": merged,
        "category": category,
    }


def pumpfun_origin(data):
    """Exige metadados explícitos de origem; endereço terminando em pump não basta."""
    nodes = [data]
    for k in ("launchpad", "platform", "pool", "token", "token_info"):
        if isinstance(data.get(k), dict):
            nodes.append(data[k])
    for node in nodes:
        for k in ("launchpad", "launchpad_name", "launchpad_platform", "launch_platform",
                  "platform", "platform_name", "creation_platform", "source_platform"):
            v = node.get(k)
            if isinstance(v, dict):
                v = v.get("name") or v.get("platform")
            if isinstance(v, str) and v.lower().replace(".", "").replace("_", "").replace("-", "").replace(" ", "") in ("pumpfun", "pump"):
                return True
    return False


def gmgn_rank_rows(data):
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("rank", "list", "tokens", "items"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return gmgn_rows(data)


def gmgn_signal_rows(data):
    rows = []
    if isinstance(data, dict):
        if data.get("token_address") or data.get("address"):
            rows.append(data)
        else:
            for value in data.values():
                rows.extend(gmgn_signal_rows(value))
    elif isinstance(data, list):
        for value in data:
            rows.extend(gmgn_signal_rows(value))
    return rows


def discover(category, chain="sol"):
    if category == "new":
        types = ("completed",)
        section = {
            "filters": ["offchain", "onchain"],
            "launchpad_platform_v2": True,
            "limit": 60,
        }
        # SOL fica restrita à Pump.fun na própria consulta e é validada
        # novamente pelos metadados do token. ETH consulta as plataformas da rede.
        if chain == "sol":
            section["launchpad_platform"] = ["Pump.fun"]
        payload = {
            "version": "v2",
            **{token_type: dict(section) for token_type in types},
        }
        data = gmgn_request(
            "/v1/trenches", method="POST",
            params={"chain": chain}, payload=payload
        )
        trench_rows = gmgn_rows(data)
        trench_unique = {}
        for row in trench_rows:
            mint = row.get("address") or row.get("token_address")
            if mint:
                key = str(mint).lower()
                existing = trench_unique.get(key)
                if existing is None or num(
                    row.get("volume_1h") or row.get("volume_24h")
                ) > num(existing.get("volume_1h") or existing.get("volume_24h")):
                    trench_unique[key] = row
        recent_rows = []
        try:
            ranked = gmgn_request(
                "/v1/market/rank",
                params={
                    "chain": chain,
                    "interval": "5m",
                    "order_by": "volume",
                    "limit": MAX_CANDIDATOS,
                },
            )
            recent_rows.extend(gmgn_rank_rows(ranked))
        except Exception as exc:
            log.warning("GMGN ranking curto %s indisponível: %s",
                        chain, str(exc)[:140])
        try:
            signals = gmgn_request(
                "/v1/market/token_signal",
                method="POST",
                payload={"chain": chain, "groups": [{"signal_type": [12]}]},
            )
            recent_rows.extend(gmgn_signal_rows(signals))
        except Exception as exc:
            log.warning("GMGN smart money %s indisponível: %s",
                        chain, str(exc)[:140])

        momentum_unique = {}
        for source in recent_rows:
            mint = source.get("address") or source.get("token_address")
            if not mint:
                continue
            row = dict(source.get("cur_data") or {})
            row.update({k: v for k, v in source.items() if k != "cur_data"})
            row["address"] = mint
            signal_types = row.get("signal_type")
            if not isinstance(signal_types, list):
                signal_types = [signal_types] if signal_types is not None else []
            row["radar_signal_types"] = sorted({
                int(num(v)) for v in signal_types if num(v) is not None
            })
            key = str(mint).lower()
            previous = momentum_unique.get(key, {})
            merged = dict(previous)
            merged.update({k: v for k, v in row.items() if v is not None})
            momentum_unique[key] = merged

        def volume_acceleration(item):
            volume_5m = num(item.get("volume_5m"))
            volume_1h = num(item.get("volume_1h"))
            volume_24h = num(item.get("volume_24h"))
            baseline = volume_1h / 12 if volume_1h > 0 else volume_24h / 288 if volume_24h > 0 else 0
            return volume_5m / baseline if volume_5m > 0 and baseline > 0 else 0

        def balanced_slice(primary, early, limit):
            chosen, seen = [], set()
            half = max(1, limit // 2)
            for bucket in (early[:half], primary[:half], early, primary):
                for item in bucket:
                    mint = item.get("address") or item.get("token_address")
                    key = str(mint).lower() if mint else ""
                    if key and key not in seen:
                        chosen.append(item)
                        seen.add(key)
                    if len(chosen) >= limit:
                        return chosen
            return chosen

        momentum_volume_rows = sorted(
            momentum_unique.values(),
            key=lambda item: (
                12 in (item.get("radar_signal_types") or []),
                num(item.get("volume_5m") or item.get("volume")),
                num(item.get("volume_1h") or item.get("volume_24h")),
            ),
            reverse=True,
        )
        momentum_accel_rows = sorted(
            momentum_unique.values(),
            key=lambda item: (
                12 in (item.get("radar_signal_types") or []),
                volume_acceleration(item),
                num(item.get("volume_5m")),
            ),
            reverse=True,
        )
        trench_volume_rows = sorted(
            trench_unique.values(),
            key=lambda item: num(item.get("volume_1h") or item.get("volume_24h")),
            reverse=True,
        )
        trench_accel_rows = sorted(
            trench_unique.values(),
            key=lambda item: (
                volume_acceleration(item),
                num(item.get("volume_5m")),
            ),
            reverse=True,
        )
        trench_candidates = balanced_slice(
            trench_volume_rows, trench_accel_rows, MAX_CANDIDATOS
        )
        momentum_candidates = balanced_slice(
            momentum_volume_rows, momentum_accel_rows,
            MAX_CANDIDATOS
        )

        selected = {}
        # Mantém o teto de candidatos e divide cada fonte entre volume absoluto
        # e aceleração relativa para alcançar rampas que ainda têm pouco volume.
        for row in trench_candidates:
            mint = row.get("address") or row.get("token_address")
            if mint:
                key = str(mint).lower()
                existing = selected.get(key, {})
                merged = dict(existing)
                merged.update({k: v for k, v in row.items() if v is not None})
                selected[key] = merged
        rows = [dict(row, radar_completed_source=True) for row in selected.values() if str(row.get("address") or row.get("token_address") or "").lower() in trench_unique]

    else:
        # Combina janela curta e tendência horária para não depender de um
        # único ranking ordenado apenas por volume.
        unique = {}
        for interval in ("5m", "1h"):
            data = gmgn_request(
                "/v1/market/rank",
                params={
                    "chain": chain,
                    "interval": interval,
                    "order_by": "volume",
                    "limit": MAX_CANDIDATOS,
                },
            )
            for row in gmgn_rank_rows(data):
                mint = row.get("address") or row.get("token_address")
                if not mint:
                    continue
                key = str(mint).lower()
                merged = dict(unique.get(key, {}))
                merged.update(row)
                intervals = set(merged.get("radar_rank_intervals") or [])
                intervals.add(interval)
                merged["radar_rank_intervals"] = sorted(intervals)
                unique[key] = merged

        # GMGN smart-money and price-spike signals add discovery coverage.
        # They affect ranking only; existing market filters remain in force.
        try:
            signal_data = gmgn_request(
                "/v1/market/token_signal",
                method="POST",
                payload={
                    "chain": chain,
                    "groups": [{"signal_type": [6, 7, 12]}],
                },
            )
            for signal in gmgn_signal_rows(signal_data):
                mint = signal.get("token_address") or signal.get("address")
                if not mint:
                    continue
                key = str(mint).lower()
                signal_types = signal.get("signal_type")
                if not isinstance(signal_types, list):
                    signal_types = [signal_types] if signal_types is not None else []
                row = dict(signal.get("cur_data") or {})
                row["address"] = mint
                row["radar_signal_types"] = sorted({
                    int(num(v)) for v in signal_types if num(v) is not None
                })
                row["radar_signal_trigger_at"] = num(signal.get("trigger_at"))
                row["radar_signal_times"] = num(signal.get("signal_times"))
                merged = dict(unique.get(key, {}))
                merged.update({k: v for k, v in row.items() if v is not None})
                types_seen = set(merged.get("radar_signal_types") or [])
                types_seen.update(row["radar_signal_types"])
                merged["radar_signal_types"] = sorted(types_seen)
                unique[key] = merged
        except Exception as exc:
            log.warning("GMGN sinais %s indisponíveis: %s", chain, str(exc)[:140])

        rows = sorted(
            unique.values(),
            key=lambda item: (
                bool(item.get("radar_signal_types")),
                num(item.get("volume_1h") or item.get("volume_24h")),
                num(item.get("volume_5m")),
            ),
            reverse=True,
        )[:MAX_CANDIDATOS]

    found = []
    for row in rows:
        mint = row.get("address") or row.get("token_address")
        if not mint:
            continue
        try:
            info = gmgn_token_info(mint, chain)
            pair = gmgn_to_pair(row, category, info, chain)
            if not pair:
                continue
            if chain == "sol" and not pumpfun_origin(pair.get("gmgn") or {}):
                continue
            found.append(pair)
        except Exception as exc:
            log.warning(
                "GMGN token info falhou (%s/%s): %s",
                chain, str(mint)[:6], str(exc)[:160]
            )
            if gmgn_block_until > time.monotonic():
                break
    return found


def gmgn_search_tokens(symbol):
    data = gmgn_request(
        "/v1/market/search",
        params={"q": symbol, "chain": "sol"},
    )
    rows = data.get("coins", []) if isinstance(data, dict) else []
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (row.get("symbol") or "").upper() != symbol.upper():
            continue
        mint = row.get("address")
        if not mint:
            continue
        try:
            info = gmgn_token_info(mint)
            pair = gmgn_to_pair(row, "new", info)
            if pair:
                found.append(pair)
        except Exception as exc:
            log.warning(
                "GMGN busca do token falhou (%s): %s",
                mint[:6], type(exc).__name__
            )
        if len(found) >= 5:
            break
    return found


def telegram_send(message):
    if not TOKEN or not CHAT_ID:
        log.error("Token ou Chat ID ausente")
        return False

    data = urlencode({
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true"
    }).encode()

    try:
        result = request_json(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data=data,
            timeout=25
        )
        return result.get("ok") is True
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            description = str(body.get("description", "sem descrição"))
        except (ValueError, UnicodeError):
            description = "resposta inválida"
        log.warning("Telegram HTTP %s: %s", exc.code, description[:240])
        return False
    except Exception as exc:
        log.warning("Falha Telegram (%s)", type(exc).__name__)
        return False


# ============================================================
# AUXILIARES
# ============================================================

def num(value, default=0.0):
    try:
        converted = float(value)
        return converted if math.isfinite(converted) else default
    except (TypeError, ValueError):
        return default


def esc(value):
    return html.escape(str(value), quote=True)


def usd(value):
    return f"US$ {num(value):,.2f}"


def price_fmt(value):
    value = num(value)
    if value <= 0:
        return "Indisponível"
    return f"US$ {Decimal(f'{value:.10g}'):f}"


def pct_fmt(value):
    return f"{value:+.2f}%"


def age_hours(pair):
    created = num(pair.get("pairCreatedAt"))
    if created <= 0:
        return None
    return max(0, (time.time() - created / 1000) / 3600)


def link(url, label):
    return f'<a href="{esc(url)}">{esc(label)}</a>'


def market_cap_fmt(value):
    return usd(value) if num(value) > 0 else "Indisponível"


def format_time(timestamp):
    return datetime.fromtimestamp(timestamp, FUSO_BRASILIA).strftime(
        "%d/%m %H:%M:%S"
    )


def gmgn_link(mint, chain="sol"):
    url = f"https://gmgn.ai/{chain}/token/" + quote(mint, safe="")
    return "🔍 " + link(url, "Abrir GMGN")


def followup_windows(record):
    # Alertas da versão anterior não registravam o par usado na entrada.
    if not record.get("pair_address"):
        return ()
    if record["category"] == "new":
        return (JANELA_SAIDA_NEW,)
    return JANELAS_PROMOVIDOS


# ============================================================
# HISTÓRICO LOCAL
# ============================================================

def save_state():
    with lock:
        try:
            STATE_FILE.parent.mkdir(
                parents=True, exist_ok=True
            )
            fd, temp_path = tempfile.mkstemp(
                prefix=".radar_",
                suffix=".tmp",
                dir=str(STATE_FILE.parent)
            )

            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())

                os.replace(temp_path, STATE_FILE)
                return True
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)

        except Exception:
            log.exception("Falha ao salvar histórico")
            return False


def load_state():
    global state

    if not STATE_FILE.exists():
        return

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            loaded = json.load(f)

        if isinstance(loaded, dict):
            with lock:
                state = {
                    "seen": loaded.get("seen", {}),
                    "records": loaded.get("records", {}),
                    "influencer_posts": loaded.get("influencer_posts", {})
                }

        log.info(
            "Histórico carregado: %s registros",
            len(state["records"])
        )
    except Exception:
        log.exception("Não foi possível ler histórico")


def cleanup():
    now = time.time()
    changed = False

    with lock:
        for mint, ts in list(state["seen"].items()):
            if now - num(ts) >= DEDUP_SEGUNDOS:
                del state["seen"][mint]
                changed = True

        # Mantém registros concluídos por 7 dias.
        for key, rec in list(state["records"].items()):
            age = now - num(rec.get("created_at"))
            checkpoints = rec.get("checkpoints", {})

            complete = all(
                str(window) in checkpoints
                for window in followup_windows(rec)
            )

            if age > 7 * 86400 and complete:
                del state["records"][key]
                changed = True

    if changed:
        save_state()


def was_sent(mint):
    with lock:
        ts = state["seen"].get(mint)
        return (
            ts is not None
            and time.time() - num(ts) < DEDUP_SEGUNDOS
        )


def register_alert(mint, pair, category):
    now = time.time()
    base = pair.get("baseToken") or {}

    record = {
        "mint": mint,
        "symbol": base.get("symbol") or "???",
        "name": base.get("name") or "Token",
        "category": category,
        "pair_address": pair["pairAddress"],
        "initial_price": num(pair.get("priceUsd")),
        "initial_market_cap": num(pair.get("marketCap")),
        "created_at": now,
        "checkpoints": {},
        "observations": {}
    }

    with lock:
        state["seen"][mint] = now
        state["records"][f"{mint}:{int(now)}"] = record

    save_state()




def filter_reason(pair, category):
    liquidity = num((pair.get("liquidity") or {}).get("usd"))
    volumes = pair.get("volume") or {}
    transactions = pair.get("txns") or {}
    changes = pair.get("priceChange") or {}
    hourly = transactions.get("h1") or {}
    volume = num(volumes.get("h1"))
    buys = num(hourly.get("buys"))
    sells = num(hourly.get("sells"))
    change = num(changes.get("h1"), default=None)

    min_volume = VOL_NEW_MIN if category == "new" else VOL_TREND_MIN
    if liquidity < LIQ_MIN:
        return "liquidez abaixo do mínimo"
    if volume < min_volume:
        return "volume 1h abaixo do mínimo"

    # Sinais de risco são informativos; não bloqueiam a oportunidade.
    """
    gmgn = pair.get("gmgn") or {}
    stat = gmgn.get("stat") or {}
    if gmgn.get("is_wash_trading") is True:
        return "wash trading sinalizado pela GMGN"
    risk = gmgn.get("rug_ratio")
    if risk is not None and num(risk) > 0.3:
        return "rug_ratio acima de 0.30"
    for key in (
        "bundler_rate", "bundler_trader_amount_rate",
        "rat_trader_amount_rate", "insider_rate",
        "top_bundler_trader_percentage", "top_rat_trader_percentage",
    ):
        value = gmgn.get(key)
        if value is None:
            value = stat.get(key)
        if value is not None and num(value) > 0.3:
            return "bundlers/insiders acima de 0.30"

    """
    if buys < BUYS_MIN:
        return "compras 1h abaixo do mínimo"
    if sells < SELLS_MIN:
        return "vendas 1h abaixo do mínimo"
    if buys < sells * BUY_RATIO_MIN:
        return "compras 1h não superam vendas pelo fator exigido"
    if change is None or not CHANGE_MIN <= change <= CHANGE_MAX:
        return "variação 1h fora do intervalo"
    if category != "new":
        if change <= 0:
            return "continuidade exige alta em 1h"
        recent_change = num(changes.get("m5"), default=None)
        if recent_change is not None and recent_change < -5:
            return "queda forte em 5m"
        return None

    age = age_hours(pair)
    if age is None or not IDADE_NEW_MIN <= age <= IDADE_NEW_MAX:
        return "idade fora de 10 minutos a 3 horas"
    if not NEW_CHANGE_MIN <= change <= NEW_CHANGE_MAX:
        return "variação 1h fora do intervalo de tokens recentes"

    total = buys + sells
    if buys / total > NEW_BUY_SHARE_MAX:
        return "proporção de compras suspeita"
    if volume / total < NEW_TICKET_MIN:
        return "ticket médio abaixo do mínimo"
    if volume / liquidity > NEW_VOLUME_LIQ_MAX:
        return "volume/liquidez acima do limite"

    recent = transactions.get("m5") or {}
    recent_buys = num(recent.get("buys"))
    recent_sells = num(recent.get("sells"))
    recent_total = recent_buys + recent_sells
    if recent_total < NEW_TXNS_5M_MIN:
        return "transações 5m abaixo do mínimo"
    if recent_buys < recent_sells or recent_sells < NEW_SELLS_5M_MIN:
        return "atividade de compras/vendas 5m insuficiente"

    recent_change = num(changes.get("m5"), default=None)
    if (
        recent_change is None
        or not 0 <= recent_change <= NEW_CHANGE_5M_MAX
        or recent_buys / recent_total > NEW_BUY_SHARE_MAX
        or num(volumes.get("m5")) / recent_total < NEW_TICKET_MIN
    ):
        return "variação ou ticket médio 5m fora do intervalo"
    return None


def passes_filters(pair, category):
    return filter_reason(pair, category) is None


# ============================================================
# ALERTA INICIAL

# ============================================================
# ALERTA INICIAL
# ============================================================

def audit_risk(pair):
    """Oito verificações; ausência de dado nunca significa segurança."""
    data = pair.get("gmgn") or {}
    if not isinstance(data, dict):
        data = {}
    sources = [data] + [
        data[k] for k in ("security", "token_security", "risk", "pool", "stat", "dev")
        if isinstance(data.get(k), dict)
    ]

    def field(*names):
        for source in sources:
            for name in names:
                if source.get(name) is not None:
                    return source[name]
        return None

    def boolean(raw):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str):
            if raw.strip().lower() in ("true", "yes", "1"):
                return True
            if raw.strip().lower() in ("false", "no", "0"):
                return False
        return None

    def percentage(*names):
        raw = num(field(*names), default=None)
        if raw is None or not 0 <= raw <= 100:
            return None
        # GMGN usa taxas fracionárias em campos *_rate.
        # Para campos *_percentage, só aceitar valores já expressos em %.
        selected = next((name for name in names if field(name) is not None), None)
        if selected and (selected.endswith("_rate") or selected.endswith("_ratio")):
            if raw > 1:
                return None
            return raw * 100
        return raw

    checks, points, confirmed = [], 0, 0

    for label, names in (
        ("NoMint", ("is_mintable", "mintable")),
        ("Blacklist", ("is_blacklisted", "blacklist")),
        ("Wash trading", ("is_wash_trading",)),
    ):
        value = boolean(field(*names))
        if value is None:
            checks.append(f"⚪ {label}: não verificado")
        else:
            confirmed += 1
            points += 20 if value else 0
            checks.append(f"{'🔴 Risco' if value else '🟢 Sem sinal'} — {label}")

    burned = percentage("lp_burned_percent", "burnt_rate")
    if burned is None:
        checks.append("⚪ LP queimado: não verificado (burn_ratio não confirma LP)")
    else:
        confirmed += 1
        points += 20 if burned < 80 else 0
        checks.append(f"{'🔴' if burned < 80 else '🟢'} LP queimado: {burned:.1f}% (não equivale a lock)")

    top = percentage("top_10_holder_rate", "top10_holder_rate", "top10_holder_percent")
    if top is None:
        checks.append("⚪ Top 10 holders: não verificado")
    else:
        confirmed += 1
        points += 20 if top > 30 else 10 if top > 15 else 0
        checks.append(f"{'🔴' if top > 30 else '🟡' if top > 15 else '🟢'} Top 10: {top:.1f}%")

    # São indicadores distintos da GMGN; não declarar equivalência entre
    # traders suspeitos, insiders e wallets que fizeram bundles.
    for label, names in (
        ("Insiders", ("insider_rate",)),
        ("Bundlers", ("bundler_rate", "bundler_trader_amount_rate", "top_bundler_trader_percentage")),
        ("Snipers", ("top70_sniper_hold_rate", "sniper_rate")),
    ):
        ratio = percentage(*names)
        if ratio is None:
            checks.append(f"⚪ {label}: não verificado")
        else:
            confirmed += 1
            points += 15 if ratio > 30 else 8 if ratio > 15 else 0
            checks.append(f"{'🔴' if ratio > 30 else '🟡' if ratio > 15 else '🟢'} {label}: {ratio:.1f}%")

    # Indicadores complementares: não entram na contagem de oito.
    checks.append("📋 Indicadores complementares GMGN:")
    for label, names in (
        ("Dev/equipe", ("dev_team_hold_rate",)),
        ("Criador", ("creator_hold_rate",)),
        ("Cofres privados", ("private_vault_hold_rate",)),
        ("Carteiras novas", ("fresh_wallet_rate",)),
        ("Traders rat GMGN (não equivalem a insiders)", ("top_rat_trader_percentage",)),
        ("Traders entrapment GMGN", ("top_entrapment_trader_percentage",)),
        ("Bot degen GMGN", ("top_bot_degen_percentage", "bot_degen_rate")),
    ):
        ratio = percentage(*names)
        if ratio is not None:
            checks.append(f"• {label}: {ratio:.1f}% (informativo)")
    for label, name in (
        ("GMGN creator_created_count (escopo não validado)", "creator_created_count"),
        ("GMGN creator_open_count (escopo não validado)", "creator_open_count"),
        ("Detentores", "holder_count"),
    ):
        value = num(field(name), default=None)
        if value is not None and value >= 0:
            checks.append(f"• {label}: {int(value)}")

    for label, names in (
        ("Honeypot", ("is_honeypot",)),
        ("Autoridade renunciada (não confirma NoMint)", ("is_renounced",)),
        ("Código aberto", ("is_open_source",)),
    ):
        value = boolean(field(*names))
        if value is not None:
            checks.append(f"• {label}: {'sim' if value else 'não'} (GMGN)")
    for label, names in (
        ("Burn ratio (não confirma LP)", ("burn_ratio",)),
        ("Queima de tokens pelo dev", ("dev_token_burn_ratio",)),
        ("Lock percent", ("lock_percent",)),
    ):
        value = percentage(*names)
        if value is not None:
            checks.append(f"• {label}: {value:.1f}% (informativo)")
    tax = num(field("buy_tax"), default=None)
    if tax is not None and tax >= 0:
        checks.append(f"• Taxa de compra GMGN: {tax:g} (unidade não validada)")
    locked = percentage("locked_ratio")
    if locked is None:
        checks.append("⚪ Liquidez bloqueada: não verificada")
    else:
        checks.append(f"🔒 Locked ratio GMGN: {locked:.1f}% (não comprova lock permanente)")
    lock_percent = percentage("lock_percent")
    if locked is not None and lock_percent is not None and abs(locked - lock_percent) >= 5:
        checks.append("⚠️ Indicadores locked_ratio e lock_percent divergentes; conferir pool")
    if locked is not None and locked < 1:
        checks.append("⚠️ Sem lock significativo indicado pela GMGN; retirada não verificada")
    checks.append("⚪ Histórico de retiradas de liquidez: não verificado")
    return (min(100, points) if confirmed else None), confirmed, checks


def gmgn_audit_enrich(pair):
    """Enriquecimento GMGN: preço, segurança explícita e estado do pool."""
    mint = (pair.get("baseToken") or {}).get("address")
    if not mint:
        return pair
    chain = {"ethereum": "eth", "bsc": "bsc"}.get(pair.get("chainId"), "sol")
    combined = dict(pair.get("gmgn") or {})
    for path, section in (
        ("/v1/token/security", "security"),
        ("/v1/token/pool_info", "pool"),
    ):
        try:
            data = gmgn_request(path, params={"chain": chain, "address": mint})
            if isinstance(data, dict):
                nested = next(
                    (data[key] for key in (section, "token_security", "token", "info")
                     if isinstance(data.get(key), dict)),
                    data,
                )
                combined[section] = nested
        except Exception as exc:
            log.warning(
                "GMGN %s falhou (%s): %s",
                section, mint[:6], str(exc)[:160]
            )
            if gmgn_block_until > time.monotonic():
                break
    pair["gmgn"] = combined
    for section in ("security", "pool", "stat", "dev", "risk"):
        node = combined.get(section)
        if isinstance(node, dict):
            log.info("GMGN auditoria %s seção %s: campos=%s",
                     mint[:6], section, sorted(str(k) for k in node.keys())[:60])
    return pair


def alert_message(pair, category):
    base = pair.get("baseToken") or {}
    mint = base.get("address") or ""
    tx = (pair.get("txns") or {}).get("h1") or {}
    age = age_hours(pair)
    change = num((pair.get("priceChange") or {}).get("h1"), default=None)
    accumulation = pair.get("accumulation_setup") or {}
    title = (
        "🚀 OPORTUNIDADE RECENTE — ALTO RETORNO POTENCIAL"
        if category == "new" and accumulation
        else "🚀 OPORTUNIDADE RECENTE" if category == "new"
        else "🔥 CONTINUIDADE DE ALTA"
    )
    data = pair.get("gmgn") or {}
    sources = [data] + [data[k] for k in ("security", "token_security", "risk", "pool", "stat", "dev") if isinstance(data.get(k), dict)]
    def field(*names):
        for source in sources:
            for name in names:
                if source.get(name) is not None:
                    return source[name]
        return None
    def flag(*names):
        v = field(*names)
        if isinstance(v, bool):
            return v
        if v in (0, 1, "0", "1"):
            return str(v) == "1"
        if isinstance(v, str) and v.lower() in ("true", "false"):
            return v.lower() == "true"
        return None
    def percent(*names):
        for name in names:
            raw = num(field(name), default=None)
            if raw is None or raw < 0:
                continue
            if name.endswith(("_rate", "_ratio")):
                if raw > 1:
                    continue
                return raw * 100
            if raw <= 100:
                return raw
        return None
    lines = [
        f"<b>🧠 RADAR {VERSAO} — {title}</b>", "",
        f"🪙 <b>{esc(base.get('name') or 'Token')} ({esc(base.get('symbol') or '???')})</b>",
        f"💵 Preço: {price_fmt(pair.get('priceUsd'))}",
        f"🏷 Market cap: {market_cap_fmt(pair.get('marketCap'))}",
        f"💧 Liquidez: {usd((pair.get('liquidity') or {}).get('usd'))}",
        f"📊 Volume 1h: {usd((pair.get('volume') or {}).get('h1'))}",
        f"🟢 Compras 1h: {int(num(tx.get('buys')))} | 🔴 Vendas: {int(num(tx.get('sells')))}",
        f"⏳ Idade: {f'{age * 60:.0f} minutos' if age is not None and age < 1 else f'{age:.1f} horas' if age is not None else 'não informada'}",
    ]
    signal_types = data.get("radar_signal_types") or []
    if signal_types:
        signal_names = []
        if 12 in signal_types:
            signal_names.append("compra de smart money")
        if 6 in signal_types:
            signal_names.append("movimento de preço")
        if 7 in signal_types:
            signal_names.append("nova máxima")
        if signal_names:
            lines.append("📡 Sinal GMGN: " + ", ".join(signal_names))
    if change is not None and -100 <= change <= 1000:
        lines.append(f"📈 Variação 1h: {pct_fmt(change)}")
    reversal = pair.get("reversal_setup") or {}
    if category == "new" and reversal:
        lines.extend([
            "",
            "<b>📉 SETUP DE REVERSÃO</b>",
            f"Queda da máxima observada até o fundo: -{reversal['drawdown_pct']:.1f}%",
            f"Repique desde o fundo: +{reversal['rebound_pct']:.1f}% "
            f"(recuperou {reversal['retracement_pct']:.1f}% da queda)",
            f"Volume recente: {reversal['volume_acceleration']:.2f}× "
            "a média dos 3 intervalos anteriores",
            f"K-line GMGN: {esc(reversal['resolution'])}",
        ])
    accumulation = pair.get("accumulation_setup") or {}
    if category == "new" and accumulation:
        lines.extend([
            "",
            (
                "<b>📈 REACUMULAÇÃO — SEGUNDA PERNA</b>"
                if accumulation.get("setup_mode") == "segunda perna"
                else "<b>📈 BASE LOCAL DESPERTANDO — SINAL ANTECIPADO</b>"
            ),
            "Volume acelerando ou sustentado; preço avançando com fundos locais preservados.",
            f"Volume médio recente: {accumulation['volume_acceleration']:.2f}× "
            "a média da base local anterior",
            f"Preço desde o fundo local: {pct_fmt(accumulation['gradual_change_pct'])}",
            *([f"Recuo após o primeiro pico: -{accumulation['second_leg_drawdown_pct']:.1f}%"]
              if accumulation.get("second_leg_drawdown_pct") is not None else []),
            *([f"⚠️ Exceção aplicada: {esc(', '.join(accumulation['override_reasons']))}."]
              if accumulation.get("override_reasons") else []),
            f"Variação curta: {pct_fmt(accumulation['short_change_pct'])}",
            "Fundo local preservado e compras recentes acima das vendas.",
            f"K-line GMGN: {esc(accumulation['resolution'])}",
        ])
    theme = viral_events.classify(pair)
    if theme:
        lines.extend(["", f"🔥 <b>RADAR DE EVENTOS VIRAIS — {esc(theme)}</b>", "🔎 Nome/símbolo relacionado ao tema; evento e causalidade não verificados.", "📊 Sinais de mercado sujeitos aos filtros normais."])
    whale_text = whale_risk.evaluate(pair, pair.get("whale_holders"))
    trader_text = gmgn_smart_money_summary(pair)
    lines.extend((["", whale_text] if whale_text else []))
    lines.extend((["", trader_text] if trader_text else []))
    lines.extend(["", "<b>🛡️ SEGURANÇA</b>"])
    for label, names in (("NoMint", ("is_mintable", "mintable")), ("Blacklist", ("is_blacklisted", "blacklist")), ("Wash trading", ("is_wash_trading",))):
        value = flag(*names)
        if value is not None:
            lines.append(f"{'🔴' if value else '🟢'} {label}: {'risco indicado' if value else 'sem sinal reportado'}")
    top = percent("top_10_holder_rate", "top10_holder_rate", "top10_holder_percent")
    if top is not None:
        lines.append(f"{'🔴' if top > 30 else '🟢'} Top 10 holders: {top:.1f}%")
    bundlers = percent("bundler_rate", "bundler_trader_amount_rate", "top_bundler_trader_percentage")
    snipers = percent("top70_sniper_hold_rate", "sniper_rate")
    if bundlers is not None:
        lines.append(f"📍 Bundlers: {bundlers:.1f}%")
    if snipers is not None:
        lines.append(f"🎯 Snipers: {snipers:.1f}%")
    # Apenas indicadores explícitos de LP; burn_ratio de tokens não comprova queima da pool.
    lp_burn = percent("lp_burned_percent", "lp_burned_rate", "lp_burn_rate")
    lp_burn_flag = flag("is_lp_burned", "lp_burned")
    if lp_burn is not None:
        lp_status = ("🟢 LP queimado: " if lp_burn >= 95 else "🔴 LP não totalmente queimado: ") + f"{lp_burn:.1f}%"
    elif lp_burn_flag is True:
        lp_status = "🟢 LP queimado: indicado pela fonte (percentual N/D)"
    elif lp_burn_flag is False:
        lp_status = "🔴 LP não queimado: indicado pela fonte"
    else:
        lp_status = None
    if lp_status:
        lines.append(lp_status)
    locked = percent("locked_ratio", "lock_percent")
    if locked is not None:
        lines.append(f"{'🔴' if locked < 1 else '🟡'} Liquidez bloqueada (indicador): {locked:.1f}%")
    lines.extend([
        "⚠️ Auditoria parcial; ausência de alerta não comprova segurança.",
        "", gmgn_link(mint, {"ethereum": "eth", "bsc": "bsc"}.get(pair.get("chainId"), "sol")), "<b>Contrato:</b>", f"<code>{esc(mint)}</code>",
        "⚠️ Não é recomendação de compra."
    ])
    return "\n".join(lines)




def gmgn_kline_history(pair, chain):
    """Busca uma série K-line por candidato para reutilizar nas duas estratégias."""
    now = int(time.time())
    created_ms = num(pair.get("pairCreatedAt"))
    created_at = created_ms / 1000 if created_ms > 0 else 0
    max_hours = 72 if chain == "eth" else 3
    start_at = max(created_at, now - max_hours * 3600) if created_at else now - max_hours * 3600
    if start_at >= now:
        return [], "histórico K-line insuficiente"

    resolution = "1m" if now - start_at <= 6 * 3600 else "5m"
    try:
        raw = gmgn_request(
            "/v1/market/token_kline",
            params={
                "chain": chain,
                "address": (pair.get("baseToken") or {}).get("address"),
                "resolution": resolution,
                "from": int(start_at),
                "to": now,
            },
        )
    except Exception as exc:
        log.warning("GMGN K-line %s/%s indisponível: %s",
                    chain, (pair.get("baseToken") or {}).get("address", "")[:8],
                    str(exc)[:140])
        return [], "K-line indisponível"

    rows = raw if isinstance(raw, list) else next(
        (raw.get(key) for key in ("list", "klines", "candles", "items")
         if isinstance(raw, dict) and isinstance(raw.get(key), list)),
        [],
    )
    candles = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        candle = {
            "time": num(row.get("time"), default=None),
            "open": num(row.get("open"), default=None),
            "high": num(row.get("high"), default=None),
            "low": num(row.get("low"), default=None),
            "close": num(row.get("close"), default=None),
            "volume": num(row.get("volume"), default=None),
        }
        if all(candle[key] is not None and candle[key] > 0
               for key in ("time", "open", "high", "low", "close")):
            candles.append(candle)
    candles.sort(key=lambda item: item["time"])
    return candles, resolution


def gmgn_cohort_candles(mint, chain, started):
    """Fetch 1m candles for one alerted cohort's first three hours."""
    start_at = int(started)
    end_at = min(int(time.time()), start_at + max(intelligence.WINDOWS))
    if end_at <= start_at:
        return []
    raw = gmgn_request(
        "/v1/market/token_kline",
        params={
            "chain": chain,
            "address": mint,
            "resolution": "1m",
            "from": start_at,
            "to": end_at,
        },
    )
    rows = raw if isinstance(raw, list) else next(
        (raw.get(key) for key in ("list", "klines", "candles", "items")
         if isinstance(raw, dict) and isinstance(raw.get(key), list)),
        [],
    )
    candles = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        candle = {
            "time": num(row.get("time"), default=None),
            "high": num(row.get("high"), default=None),
            "low": num(row.get("low"), default=None),
        }
        if candle["time"] is not None and candle["high"] and candle["low"]:
            candles.append(candle)
    candles.sort(key=lambda item: item["time"])
    return candles


def gmgn_trader_rows(node):
    """Extract the trader records from common GMGN response envelopes."""
    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)]
    if isinstance(node, dict):
        for key in ("list", "traders", "items", "data", "result"):
            if key in node:
                found = gmgn_trader_rows(node[key])
                if found:
                    return found
        if node.get("address") and (
            "buy_volume_cur" in node or "sell_volume_cur" in node
        ):
            return [node]
    return []


def gmgn_smart_money_summary(pair):
    rows = gmgn_trader_rows(pair.get("gmgn_top_traders"))
    if not rows:
        return ""
    comparable = []
    for item in rows:
        buys = num(item.get("buy_volume_cur"), default=None)
        sells = num(item.get("sell_volume_cur"), default=None)
        if buys is not None and sells is not None:
            comparable.append((buys,sells))
    buying = sum(buy>sells for buy,sells in comparable)
    selling = sum(sell>buy for buy,sell in comparable)
    tied = len(comparable)-buying-selling
    text = (
        f"🧠 <b>TOP TRADERS GMGN — filtro smart_degen</b>\n"
        f"Registros: {len(rows)}"
    )
    if comparable:
        text += (
            f" | compra maior: {buying} | venda maior: {selling}"
            + (f" | empate: {tied}" if tied else "")
            + f" (campos comparáveis: {len(comparable)})"
        )
    text += "\nℹ️ Consulta indicativa; não demonstra controle comum entre carteiras."
    return text


def gmgn_reversal_setup(pair, candles, resolution):
    """Recuo de 50–80% desde a máxima observada, seguido por repique confirmado."""
    if len(candles) < 10:
        return None, "histórico K-line curto demais"
    peak_index = max(range(len(candles)), key=lambda i: candles[i]["high"])
    peak = candles[peak_index]["high"]
    later = candles[peak_index + 1:]
    if len(later) < 4:
        return None, "sem recuo e repique confirmados após a máxima"
    trough_rel = min(range(len(later)), key=lambda i: later[i]["low"])
    trough_index = peak_index + 1 + trough_rel
    trough = candles[trough_index]["low"]
    current = num(pair.get("priceUsd"), default=None) or candles[-1]["close"]
    if peak <= 0 or trough <= 0:
        return None, "máxima ou fundo inválidos"

    drawdown = (peak - trough) / peak * 100
    if not 50 <= drawdown <= 80:
        return None, "queda desde a máxima fora da faixa de 50–80%"
    if time.time() - candles[trough_index]["time"] > 3600:
        return None, "fundo antigo; repique não é recente"

    rebound = (current / trough - 1) * 100
    retracement = (current - trough) / (peak - trough) * 100
    if rebound < 15 or retracement < 20:
        return None, "repique ainda sem força mínima"
    if current >= peak * 0.95:
        return None, "preço já muito próximo da máxima"

    recent = candles[-4:]
    positive_steps = sum(
        recent[i]["close"] > recent[i - 1]["close"]
        for i in range(1, len(recent))
    )
    if recent[-1]["close"] <= recent[-2]["close"] or positive_steps < 2:
        return None, "candles ainda não confirmam retomada"

    prior, latest = candles[-6:-3], candles[-3:]
    if len(prior) < 3 or len(latest) < 3 or any(
        c["volume"] is None for c in prior + latest
    ):
        return None, "volume K-line insuficiente para confirmar repique"
    prior_usd = sum(c["volume"] * c["close"] for c in prior) / 3
    latest_usd = sum(c["volume"] * c["close"] for c in latest) / 3
    acceleration = latest_usd / prior_usd if prior_usd > 0 else 0
    if acceleration < 1.2:
        return None, "volume não acelerou junto ao repique"

    top = max(c["high"] for c in candles[-5:])
    bottom = min(c["low"] for c in candles[-5:])
    if current >= peak * 0.90 and (top - bottom) / current * 100 <= 8:
        return None, "lateralização detectada perto da máxima"

    return {
        "drawdown_pct": drawdown,
        "rebound_pct": rebound,
        "retracement_pct": retracement,
        "volume_acceleration": acceleration,
        "resolution": resolution,
    }, "reversão confirmada"


def gmgn_accumulation_setup(pair, candles, resolution, features, strategy):
    """Procura atividade e preço subindo gradualmente antes de uma disparada."""
    config = intelligence.CONFIG[strategy]
    age = features.get("age")
    cap = features.get("cap")
    liquidity = features.get("liq")
    price = features.get("price")
    sol_early_pattern = pair.get("chainId") == "solana" and strategy == "new"
    cap_floor = 3000 if sol_early_pattern else config["cap"][0]
    liquidity_floor = 10000 if sol_early_pattern else config["liq"]
    if age is None or not config["age"][0] <= age <= config["age"][1]:
        return None, "idade fora do filtro de oportunidade recente"
    if cap is None or not cap_floor <= cap <= config["cap"][1]:
        return None, "market cap fora do filtro do setup"
    if liquidity is None or liquidity < liquidity_floor:
        return None, "liquidez abaixo do piso do setup"
    if price is None or price <= 0:
        return None, "preço indisponível"
    if len(candles) < 12:
        return None, "histórico curto para detectar acumulação"

    # Usa até 24 candles e compara os últimos 5 com a base local anterior.
    # Com 12 candles já permite identificar uma escada em token recém-lançado.
    bars = candles[-24:]
    recent = bars[-15:] if len(bars) >= 15 else bars
    volume_windows = (recent[:-5], recent[-5:])
    if any(c["volume"] is None for group in volume_windows for c in group):
        return None, "volume K-line indisponível para acumulação"
    baseline_volume = sum(c["volume"] * c["close"] for c in volume_windows[0]) / len(volume_windows[0])
    recent_volume = sum(c["volume"] * c["close"] for c in volume_windows[1]) / len(volume_windows[1])
    if baseline_volume <= 0:
        return None, "volume de referência insuficiente"
    acceleration = recent_volume / baseline_volume
    volume_accelerating = acceleration >= 1.15
    if acceleration < 0.75:
        return None, "volume local está secando"

    # Compara o preço com o fundo local anterior, não com o início do token.
    local_base = min(c["low"] for c in recent[:-5])
    gradual_change = (price / local_base - 1) * 100 if local_base > 0 else -1
    if not 2 <= gradual_change <= 100:
        return None, "preço sem rampa local ou já muito esticado"

    short_index = -4 if resolution == "1m" else -3
    short_change = (price / bars[short_index]["close"] - 1) * 100
    short_ceiling = 12 if resolution == "1m" else 18

    support_window = recent[-10:]
    previous_lows = [c["low"] for c in support_window[:5]]
    latest_lows = [c["low"] for c in support_window[5:]]
    if not previous_lows or not latest_lows:
        return None, "base local curta demais"
    prior_floor = min(previous_lows)
    latest_floor = min(latest_lows)
    higher_lows = latest_floor >= prior_floor * 0.98
    if not higher_lows:
        return None, "base local ainda perde fundos"

    last = bars[-5:]
    positive_steps = sum(
        last[i]["close"] > last[i - 1]["close"]
        for i in range(1, len(last))
    )
    if positive_steps < 2 or last[-1]["close"] <= last[-2]["close"]:
        return None, "candles recentes não confirmam retomada da base"

    previous_highs = [c["high"] for c in support_window[:5]]
    latest_highs = [c["high"] for c in support_window[5:]]
    higher_highs = max(latest_highs) >= max(previous_highs) * 1.01
    staircase = higher_lows and higher_highs and positive_steps >= 2

    pullback_pct = 0
    second_leg = False
    if len(bars) >= 24:
        first_leg = bars[:12]
        base_after_peak = bars[12:20]
        prior_peak = max(c["high"] for c in first_leg)
        second_leg_base = min(c["low"] for c in base_after_peak)
        pullback_pct = (prior_peak - second_leg_base) / prior_peak * 100 if prior_peak > 0 else 0
        recovery_pct = (price / second_leg_base - 1) * 100 if second_leg_base > 0 else -1
        prior_micro_high = max(c["high"] for c in bars[-8:-3])
        local_breakout = price >= prior_micro_high * 1.01
        second_leg = (
            15 <= pullback_pct <= 65
            and 5 <= recovery_pct <= 100
            and local_breakout
            and higher_lows
            and positive_steps >= 2
        )

    short_ceiling_second_leg = 15 if resolution == "1m" else 22
    short_move_acceptable = -2 <= short_change <= short_ceiling
    if second_leg:
        short_move_acceptable = short_move_acceptable or (
            -2 <= short_change <= short_ceiling_second_leg
        )
    if not short_move_acceptable:
        return None, "movimento curto já acelerou demais ou perdeu força"

    if not volume_accelerating and not staircase and not second_leg:
        return None, "volume sem aceleração e sem retomada após a base"

    tx = pair.get("txns") or {}
    tx5 = tx.get("m5") or {}
    buys, sells = num(tx5.get("buys")), num(tx5.get("sells"))
    if buys < 2 or sells < 1 or buys <= sells:
        return None, "compras recentes não superam as vendas"

    peak = max(c["high"] for c in candles)
    top = max(c["high"] for c in candles[-8:])
    bottom = min(c["low"] for c in candles[-8:])
    if price >= peak * 0.90 and (top - bottom) / price * 100 <= 10:
        return None, "lateralização detectada perto da máxima"

    score = 0
    score += 20 if acceleration >= 1.50 else 15 if volume_accelerating else 12
    score += 15 if higher_lows and (higher_highs or second_leg) else 10 if higher_lows else 0
    score += 15 if 2 <= gradual_change <= 35 else 10
    score += 10 if 0 <= short_change <= 8 else 5
    score += 15 if buys >= sells * 1.25 else 10
    score += 10 if liquidity >= liquidity_floor * 1.5 else 5
    return {
        "score": min(100, score),
        "volume_acceleration": acceleration,
        "gradual_change_pct": gradual_change,
        "short_change_pct": short_change,
        "staircase_confirmed": staircase,
        "setup_mode": "segunda perna" if second_leg else "escada" if staircase else "volume crescente",
        "second_leg_drawdown_pct": pullback_pct if second_leg else None,
        "resolution": resolution,
    }, "segunda perna após reacumulação" if second_leg else "base local despertando"


def migrated_wallet_limits(pair):
    data=pair.get("gmgn") or {}
    sources=[data]+[data[k] for k in ("stat","security","token_security","risk","dev") if isinstance(data.get(k),dict)]
    checks={"top10":("top_10_holder_rate","top10_holder_rate","top10_holder_percent"),"bundlers":("bundler_rate","bundler_trader_amount_rate","top_bundler_trader_percentage"),"snipers":("top70_sniper_hold_rate","sniper_rate")}
    for label,names in checks.items():
        value=None
        for name in names:
            for source in sources:
                if source.get(name) is not None:
                    raw=num(source[name],default=None)
                    if raw is not None and raw>=0:
                        value=raw*100 if name.endswith("_rate") and raw<=1 else raw if not name.endswith("_rate") and raw<=100 else None
                    break
            if value is not None: break
        if value is None: return False,label+": percentual não verificável"
        if value>=30: return False,label+": limite de 30% atingido"
    # Only checks demonstrated by GMGN: unknown wash-trading is not safe.
    wash=None
    for source in sources:
        raw=source.get("is_wash_trading")
        if isinstance(raw,bool):
            wash=raw;break
        if raw in (0,1,"0","1","true","false","True","False"):
            wash=str(raw).lower() in ("1","true");break
    if wash is None: return False,"wash trading: não verificável"
    if wash: return False,"wash trading detectado"
    return True,"ok"


def run_radar(category, chain="sol"):
    strategy = f"{chain}_{category}" if chain in ("bsc", "eth") else category
    stats = {"candidates": 0, "approved": 0, "duplicates": 0, "alerts": 0}
    rejections = {}
    try:
        candidates = discover(category, chain)
        stats["candidates"] = len(candidates)
        approved = []
        for pair in candidates:
            mint = (pair.get("baseToken") or {}).get("address")
            if not mint:
                continue
            if category == "new" and (
                was_sent(f"{chain}:{mint}") or was_sent(mint)
                or intelligence.recently_sent(chain, mint)
            ):
                stats["duplicates"] += 1
                continue

            if category != "new" or not (pair.get("gmgn") or {}).get("radar_completed_source"):
                continue
            age=age_hours(pair)
            if age is None or not (5/60 <= age <= 2):
                rejections["idade fora de 5 a 120 minutos"]=rejections.get("idade fora de 5 a 120 minutos",0)+1
                continue
            tx=(pair.get("txns") or {}).get("h1") or {}
            buys=num(tx.get("buys"),default=None)
            sells=num(tx.get("sells"),default=None)
            if buys is None or sells is None or buys<=sells:
                rejections["compras não superam vendas"]=rejections.get("compras não superam vendas",0)+1
                continue
            passed, audit_reason = migrated_wallet_limits(pair)
            if not passed:
                rejections[audit_reason] = rejections.get(audit_reason,0)+1
                continue
            score, eligible, reason, f = intelligence.evaluate(
                pair, strategy, age_hours(pair)
            )
            # New migrated-only strategy: safety, age and buy pressure were
            # checked above. Legacy reversal/accumulation and score thresholds
            # must not silently block these user-selected opportunities.
            eligible = f.get("price") is not None and f["price"] > 0
            reason = "migração concluída; segurança e compras verificadas" if eligible else "preço inválido"
            try:
                intelligence.record(mint, strategy, score, eligible, reason, f)
            except Exception as exc:
                log.warning("Histórico intelligence: %s", exc)
            if not eligible:
                rejections[reason] = rejections.get(reason, 0) + 1
                continue
            stats["approved"] += 1
            if (was_sent(f"{chain}:{mint}") or was_sent(mint)
                    or intelligence.recently_sent(chain, mint)):
                stats["duplicates"] += 1
                continue
            signal_types = (pair.get("gmgn") or {}).get("radar_signal_types") or []
            signal_bonus = 6 if 12 in signal_types else 3 if any(v in signal_types for v in (6, 7)) else 0
            theme_bonus = 0  # Tema não aumenta score sem evidência independente.
            final_score = min(100, score + signal_bonus + theme_bonus)
            # Strict paper/alert threshold, including high-return pattern overrides.
            if final_score <= 60:
                rejections["pontuação de 60 ou menos"] = rejections.get("pontuação de 60 ou menos", 0) + 1
                continue
            alert_priority = 1 if f.get("pattern_override") else 0
            approved.append((alert_priority, final_score, mint, pair, f))
        approved.sort(key=lambda item: (item[0], item[1]), reverse=True)
        for _priority, score, mint, pair, f in approved[:MAX_ALERTAS]:
            if was_sent(f"{chain}:{mint}") or was_sent(mint) or intelligence.recently_sent(chain, mint):
                continue
            gmgn_audit_enrich(pair)
            try:
                pair["whale_holders"] = gmgn_request(
                    "/v1/market/token_top_holders",
                    params={"chain": chain, "address": mint}
                )
            except Exception as exc:
                log.warning("GMGN top holders %s/%s indisponível: %s",
                            chain, mint[:8], str(exc)[:150])
            if pair.get("accumulation_setup"):
                try:
                    pair["gmgn_top_traders"] = gmgn_request(
                        "/v1/market/token_top_traders",
                        params={
                            "chain": chain,
                            "address": mint,
                            "limit": 20,
                            "tag": "smart_degen",
                        },
                    )
                except Exception as exc:
                    log.warning("GMGN smart money %s/%s indisponível: %s",
                                chain, mint[:8], str(exc)[:150])
            msg = alert_message(pair, category)
            msg = f"🌐 <b>REDE: {chain.upper()}</b>\n" + msg
            msg = f"⭐ <b>Oportunidade: {score}/100</b> (experimental)\n" + msg
            if not telegram_send(msg):
                break
            # The Telegram message was accepted. Persist its paper signal before
            # registering the alert, and report any delivery failure explicitly.
            record = {"chain":chain,"address":mint,"category":category,"score":score,
                      "pumpfun_confirmed":chain=="sol" and pumpfun_origin(pair.get("gmgn") or {}),
                      "price":num(pair.get("priceUsd")),
                      "market_cap":num(pair.get("marketCap")),
                      "liquidity":num((pair.get("liquidity") or {}).get("usd")),
                      "symbol":(pair.get("baseToken") or {}).get("symbol"),
                      "name":(pair.get("baseToken") or {}).get("name"),
                      "timestamp":time.time()}
            signal_path = os.getenv("RADAR_SIGNAL_FILE","/tmp/gmgn_radar_signals.jsonl")
            signal_written = False
            for attempt in range(3):
                try:
                    with open(signal_path,"a",encoding="utf-8") as journal:
                        journal.write(json.dumps(record,ensure_ascii=False)+"\n")
                        journal.flush()
                        os.fsync(journal.fileno())
                    signal_written = True
                    log.info("PAPER SIGNAL WRITTEN %s:%s score=%s path=%s",chain,mint,score,signal_path)
                    break
                except Exception:
                    log.exception("PAPER SIGNAL DELIVERY FAILED attempt=%d token=%s:%s",attempt+1,chain,mint)
                    time.sleep(0.5)
            if not signal_written:
                log.error("TELEGRAM ALERT WITHOUT PAPER SIGNAL %s:%s",chain,mint)
            register_alert(f"{chain}:{mint}", pair, category)
            try:
                intelligence.mark_sent(chain, mint)
                intelligence.start_cohort(mint, strategy, f)
            except Exception as exc:
                log.warning("Registro intelligence %s: %s", mint[:8], exc)
            stats["alerts"] += 1
            time.sleep(2)
    except Exception as exc:
        log.warning("%s/%s: %s", chain, category, exc)
    finally:
        log.info("%s/%s: candidatos=%d aprovados=%d duplicados=%d alertas=%d rejeicoes=%s",
                 chain, category, stats["candidates"], stats["approved"], stats["duplicates"],
                 stats["alerts"], json.dumps(rejections, ensure_ascii=False))


def learn_outcomes():
    """Acompanhamento interno das coortes; não envia atualizações ao Telegram."""
    for mint, category, started, price, horizon in intelligence.due(limit=5):
        try:
            info = gmgn_token_info(mint, "eth" if category.startswith("eth_") else "bsc" if category.startswith("bsc_") else "sol")
            current = num((info.get("price") or {}).get("price"), default=None) if isinstance(info.get("price"), dict) else num(info.get("price"), default=None)
            intelligence.settle(mint, category, started, price, horizon, current)
        except Exception as exc:
            log.warning("Coleta de resultado %s: %s", mint[:6], exc)
            if gmgn_block_until > time.monotonic():
                break

    # Once a pattern has aged three hours, one candle query reconstructs
    # its favorable/adverse excursion at every supported horizon.
    for mint, strategy, started, price in intelligence.path_due(limit=4):
        chain = "eth" if strategy.startswith("eth_") else "bsc" if strategy.startswith("bsc_") else "sol"
        try:
            candles = gmgn_cohort_candles(mint, chain, started)
            if not intelligence.record_path_metrics(
                mint, strategy, started, price, candles
            ):
                log.info("Trajetória sem candles para %s; nova tentativa limitada a 3",
                         mint[:8])
        except Exception as exc:
            intelligence.record_path_metrics(mint, strategy, started, price, [])
            log.warning("Trajetória GMGN %s/%s indisponível: %s",
                        chain, mint[:8], str(exc)[:140])
            if gmgn_block_until > time.monotonic():
                break
    log.info("Intelligence 30min: %s | trajetória 30min: %s",
             intelligence.summary(), intelligence.path_summary(1800))




# ============================================================
# CONSULTA DE PREÇOS PARA ACOMPANHAMENTO
# ============================================================

def current_prices(mints):
    prices = {}
    for mint in mints:
        try:
            info = gmgn_token_info(mint)
            pair = gmgn_to_pair(info, "new", info)
            if not pair or num(pair.get("priceUsd")) <= 0:
                continue
            prices[mint] = {
                "price": num(pair.get("priceUsd")),
                "market_cap": num(pair.get("marketCap")),
                "liquidity": num(
                    (pair.get("liquidity") or {}).get("usd")
                ),
                "observed_at": time.time(),
            }
        except Exception as exc:
            log.warning(
                "Cotação GMGN indisponível (%s): %s",
                mint[:6], type(exc).__name__
            )
            if gmgn_block_until > time.monotonic():
                break
    return prices


def checkpoint_message(rec, window, current):
    is_new = rec["category"] == "new"
    if is_new:
        title = "VENDA SIMULADA • 30 MIN"
        initial_label, current_label = "Preço de entrada", "Preço de saída"
    else:
        title = f"PROMOVIDO • ACOMPANHAMENTO {window // 3600}H"
        initial_label, current_label = "Preço no alerta", "Preço consultado"

    entry_time = rec["created_at"]
    target = entry_time + window
    lines = [
        f"📊 <b>RADAR {VERSAO} — {title}</b>",
        "",
        f"🪙 <b>{esc(rec['name'])} ({esc(rec['symbol'])})</b>",
        f"💵 {initial_label}: {price_fmt(rec['initial_price'])}",
        f"🏷 Market cap inicial: {market_cap_fmt(rec.get('initial_market_cap'))}",
        f"🕒 Alerta: {format_time(entry_time)} (Brasília)",
        f"🎯 Horário-alvo: {format_time(target)}",
    ]

    if "price" in current:
        price = current["price"]
        change = (price / rec["initial_price"] - 1) * 100
        lines.extend([
            f"🕒 Cotação consultada: {format_time(current['observed_at'])}",
            f"💵 {current_label}: {price_fmt(price)}",
            f"🏷 Market cap consultado: {market_cap_fmt(current.get('market_cap'))}",
            f"💧 Liquidez consultada: {usd(current.get('liquidity'))}",
            f"📈 Variação bruta de preço: <b>{pct_fmt(change)}</b>",
            "ℹ️ Variação pontual, sem taxas e slippage.",
        ])
    else:
        lines.append(
            "⚪ Saída sem resultado: cotação do mesmo par indisponível "
            "nos 2 min após o horário-alvo."
        )

    lines.extend(["", gmgn_link(rec["mint"]), f"<code>{esc(rec['mint'])}</code>"])
    return "\n".join(lines)


def run_followups():
    now = time.time()
    due = []
    for rec in state["records"].values():
        for window in followup_windows(rec):
            key = str(window)
            target = rec["created_at"] + window
            if key not in rec["checkpoints"] and now >= target:
                due.append((rec, key, target, window))

    if not due:
        return

    pending_mints = list(dict.fromkeys(
        rec["mint"]
        for rec, key, target, window in due
        if key not in rec.get("observations", {})
        and now <= target + (
            TOLERANCIA_SAIDA_NEW if rec["category"] == "new"
            else TOLERANCIA_PROMOVIDOS
        )
    ))
    prices = {}
    if pending_mints:
        try:
            prices = current_prices(pending_mints)
        except Exception as exc:
            log.warning("Acompanhamento: %s", exc)

    # Grava a cotação antes de enviar. Uma falha no Telegram não troca
    # o preço da saída simulada por uma cotação mais tardia.
    changed = False
    for rec, key, target, window in due:
        observations = rec.setdefault("observations", {})
        if key in observations:
            continue

        is_new = rec["category"] == "new"
        tolerance = TOLERANCIA_SAIDA_NEW if is_new else TOLERANCIA_PROMOVIDOS
        deadline = target + tolerance
        current = prices.get(rec["mint"])
        if current and target <= current["observed_at"] <= deadline:
            observations[key] = current
            changed = True
        elif time.time() > deadline:
            if is_new:
                observations[key] = {"status": "unavailable"}
            else:
                rec["checkpoints"][key] = {"status": "unavailable"}
                log.info("Acompanhamento %sh perdido para %s", window // 3600, rec["mint"])
            changed = True

    # Exige um estado gravado também em tentativas após falha anterior no disco.
    has_messages = any(key in rec["observations"] for rec, key, _, _ in due)
    if (changed or has_messages) and not save_state():
        return

    sent_count = 0
    for rec, key, _, window in due:
        if sent_count >= MAX_ALERTAS:
            break
        current = rec["observations"].get(key)
        if current is None:
            continue
        if not telegram_send(checkpoint_message(rec, window, current)):
            break
        rec["checkpoints"][key] = {**current, "sent_at": time.time()}
        del rec["observations"][key]
        save_state()
        sent_count += 1


# ============================================================
# PAPER TRADING — GMGN AO VIVO, SEM ENVIO DE ORDENS
# ============================================================

def paper_quote(chain, mint, force_refresh=False):
    """Lê somente informação/preço/liquidez da GMGN; nunca chama API de swap."""
    if force_refresh:
        gmgn_info_cache.pop(f"{chain}:{mint}", None)
    info = gmgn_token_info(mint, chain)
    pair = gmgn_to_pair(info, "paper", info, chain)
    if not pair:
        raise RuntimeError("A GMGN não retornou dados suficientes para este contrato.")
    meta = pair.get("gmgn") or {}
    launchpad = ""
    for key in ("launchpad", "launchpad_name", "launchpad_platform",
                "platform", "platform_name", "creation_platform", "source_platform"):
        value = meta.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("platform")
        if value:
            launchpad = str(value)
            break
    return {
        "chain": chain,
        "price": num(pair.get("priceUsd")),
        "liquidity": num((pair.get("liquidity") or {}).get("usd")),
        "name": (pair.get("baseToken") or {}).get("name") or "Token",
        "symbol": (pair.get("baseToken") or {}).get("symbol") or "???",
        "launchpad": launchpad,
        "observed_at": time.time(),
    }


def radar_performance_report():
    """Relatório sob demanda; não envia notificações automáticas."""
    cutoff = int(time.time()) - 7 * 86400
    try:
        with intelligence.db() as con:
            candidate_rows = con.execute(
                """SELECT strategy, eligible, reason, COUNT(*)
                   FROM candidates WHERE observed >= ?
                   GROUP BY strategy, eligible, reason
                   ORDER BY strategy, eligible DESC, COUNT(*) DESC""",
                (cutoff,),
            ).fetchall()
            outcome_rows = con.execute(
                """SELECT strategy, COUNT(*),
                          SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END),
                          AVG(return_pct)
                   FROM outcomes
                   WHERE horizon = 1800 AND observed >= ?
                     AND status = 'observed'
                   GROUP BY strategy ORDER BY strategy""",
                (cutoff,),
            ).fetchall()
    except Exception as exc:
        log.warning("Relatório do radar indisponível: %s", str(exc)[:150])
        return "📊 Relatório ainda indisponível. Aguarde o radar registrar dados."

    names = {
        "new": "SOL • recentes",
        "trending": "SOL • continuidade",
        "eth_new": "ETH • recentes",
        "eth_trending": "ETH • continuidade",
        "bsc_new": "BSC • recentes",
        "bsc_trending": "BSC • continuidade",
    }
    by_strategy = {}
    for strategy, eligible, reason, count in candidate_rows:
        row = by_strategy.setdefault(
            strategy, {"total": 0, "approved": 0, "rejected": []}
        )
        row["total"] += int(count)
        if eligible:
            row["approved"] += int(count)
        elif reason:
            row["rejected"].append((str(reason), int(count)))

    lines = [
        f"📊 <b>RADAR {VERSAO} — RELATÓRIO (7 DIAS)</b>",
        "",
        "Candidatos avaliados da GMGN, incluindo rejeitados; duplicados são ignorados:",
    ]
    if by_strategy:
        for strategy, row in sorted(by_strategy.items()):
            label = esc(names.get(strategy, strategy))
            lines.append(
                f"• <b>{label}</b>: {row['total']} avaliados; "
                f"{row['approved']} aprovados; "
                f"{row['total'] - row['approved']} rejeitados"
            )
            for reason, count in row["rejected"][:2]:
                lines.append(f"  ↳ {count}× {esc(reason)}")
    else:
        lines.append("• Ainda não há candidatos registrados nesse período.")

    outcomes = {
        row[0]: (int(row[1]), int(row[2] or 0), num(row[3], default=None))
        for row in outcome_rows
    }
    lines.extend([
        "",
        "Alertas com resultado após 30 min:",
        "Retorno bruto: não desconta taxas, gás ou slippage.",
    ])
    if outcomes:
        for strategy, (count, positive, average) in sorted(outcomes.items()):
            label = esc(names.get(strategy, strategy))
            rate = positive / count * 100 if count else 0
            avg_text = f"{average:+.2f}%" if average is not None else "indisponível"
            lines.append(
                f"• <b>{label}</b>: {count} resultados; "
                f"{positive}/{count} positivos ({rate:.0f}%); média {avg_text}"
            )
    else:
        lines.append("• Ainda não há resultados de 30 min disponíveis.")

    lines.extend([
        "",
        "Amostras descritivas; não alteram filtros nem significam previsão de lucro.",
    ])
    report = "\n".join(lines)
    return report if len(report) <= 3900 else report[:3880] + "\n…"


def handle_paper_command(update):
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if str(chat.get("id", "")) != str(CHAT_ID):
        return
    raw = str(message.get("text") or "").strip()
    if not raw:
        return
    parts = raw.split()
    command = parts[0].split("@", 1)[0].lower()
    if command == "/radar_relatorio":
        telegram_send(radar_performance_report())
        return
    if command not in ("/paper", "/paper_ajuda", "/paper_saldo",
                       "/paper_comprar", "/paper_vender",
                       "/paper_historico", "/paper_reset"):
        return

    try:
        if command == "/paper_ajuda" or (command == "/paper" and len(parts) == 1):
            reply = paper_trading.help_text()
        elif command == "/paper_saldo" or (command == "/paper" and len(parts) > 1 and parts[1].lower() in ("saldo", "carteira")):
            reply = paper_trading.portfolio(paper_quote)
        elif command == "/paper_historico" or (command == "/paper" and len(parts) > 1 and parts[1].lower() in ("historico", "histórico")):
            reply = paper_trading.history()
        elif command == "/paper_reset" or (command == "/paper" and len(parts) > 1 and parts[1].lower() in ("reset", "reiniciar")):
            args = parts[1:] if command == "/paper" else parts[1:]
            if len(args) != 2:
                raise ValueError("Use /paper reset VALOR_USDT.")
            reply = paper_trading.reset(float(args[1]))
        elif command == "/paper_comprar" or (command == "/paper" and len(parts) > 1 and parts[1].lower() in ("comprar", "buy")):
            args = parts[1:] if command == "/paper" else parts[1:]
            if len(args) != 4:
                raise ValueError("Use /paper comprar SOL|ETH CONTRATO VALOR_USDT.")
            chain = "sol" if args[1].lower() in ("sol", "solana") else "eth" if args[1].lower() in ("eth", "ethereum") else args[1].lower()
            mint, amount = args[2], float(args[3])
            reply = paper_trading.buy(chain, mint, amount, paper_quote(chain, mint, force_refresh=True))
        elif command == "/paper_vender" or (command == "/paper" and len(parts) > 1 and parts[1].lower() in ("vender", "sell")):
            args = parts[1:] if command == "/paper" else parts[1:]
            if len(args) not in (3, 4):
                raise ValueError("Use /paper vender SOL|ETH CONTRATO [PERCENTUAL].")
            chain = "sol" if args[1].lower() in ("sol", "solana") else "eth" if args[1].lower() in ("eth", "ethereum") else args[1].lower()
            mint = args[2]
            percent = float(args[3]) if len(args) == 4 else 100.0
            reply = paper_trading.sell(chain, mint, paper_quote(chain, mint, force_refresh=True), percent)
        else:
            reply = paper_trading.help_text()
    except Exception as exc:
        reply = "⚠️ " + esc(str(exc)[:500])
    telegram_send(reply)


def telegram_command_loop():
    """Consome somente comandos do chat privado configurado."""
    if not TOKEN or not CHAT_ID:
        return
    offset = paper_trading.get_update_offset()
    if offset is None:
        # Descarta comandos antigos da fila no primeiro arranque para evitar
        # executar mensagens enviadas antes de ativar o simulador.
        try:
            bootstrap = request_json(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset=-1&timeout=0",
                timeout=10,
            )
            previous = bootstrap.get("result") or []
            offset = int(previous[-1]["update_id"]) + 1 if previous else 0
            paper_trading.set_update_offset(offset)
        except Exception as exc:
            log.warning("Paper Telegram inicialização: %s", str(exc)[:160])
            offset = 0

    while True:
        try:
            query = urlencode({
                "offset": offset,
                "timeout": 20,
                "allowed_updates": json.dumps(["message"]),
            })
            data = request_json(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates?{query}",
                timeout=25,
            )
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise RuntimeError("resposta inválida do Telegram getUpdates")
            for update in data.get("result") or []:
                handle_paper_command(update)
                offset = int(update.get("update_id", offset)) + 1
                paper_trading.set_update_offset(offset)
        except Exception as exc:
            message = str(exc)
            if "409" in message and "Conflict" in message:
                log.error("Paper Telegram pausado: getUpdates indisponível (webhook ou outro consumidor ativo).")
                time.sleep(30)
            else:
                log.warning("Paper Telegram: %s", message[:160])
                time.sleep(5)


# ============================================================
# LOOP
# ============================================================

def main():
    load_state()
    threading.Thread(target=telegram_command_loop, daemon=True).start()
    telegram_send(
        f"🤖 <b>RADAR SOLANA PUMP.FUN {VERSAO} ATIVO</b>\n"
        "🚀 Oportunidades recentes e 🔥 continuidade de alta\n"
        "🛡️ Risco informativo e auditoria parcial\n"
        "🧪 Simulador de ordens GMGN disponível: /paper\n"
        "⚠️ Carteira fictícia; nenhuma ordem real é enviada\n"
        "🔁 Deduplicação: 12 horas"
    )
    next_new = time.monotonic()
    next_promoted = next_new + 900
    while True:
        now = time.monotonic()
        if now >= next_new:
            cleanup()
            run_radar("new", "sol")
            try:
                learn_outcomes()
            except Exception as exc:
                log.warning("Aprendizado: %s", exc)
            next_new = time.monotonic() + INTERVALO
        if time.monotonic() >= next_promoted:
            next_promoted = time.monotonic() + max(900, INTERVALO)
        time.sleep(max(1, min(next_new, next_promoted) - time.monotonic()))


if __name__ == "__main__":
    if os.getenv("RADAR_EMBEDDED") != "1":
        threading.Thread(target=start_server, daemon=True).start()

    if not TOKEN or not CHAT_ID or not GMGN_API_KEY:
        log.error(
            "Configure TELEGRAM_TOKEN, TELEGRAM_CHAT_ID e GMGN_API_KEY"
        )
    else:
        main()
