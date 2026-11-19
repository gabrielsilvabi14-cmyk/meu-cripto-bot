"""GMGN Trader 1.0 - protected personal dashboard; paper mode only."""
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

PASSWORD = os.getenv("TRADER_PASSWORD", "")
SECRET = os.getenv("SESSION_SECRET", "")
if len(PASSWORD) < 16 or len(SECRET) < 32:
    raise RuntimeError("TRADER_PASSWORD must have 16+ chars; SESSION_SECRET must have 32+ chars")

app = FastAPI(
    title="GMGN Trader 1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET,
    https_only=os.getenv("COOKIE_SECURE", "true").lower() == "true",
    same_site="strict",
    max_age=3600,
)
BASE = Path(__file__).resolve().parent
FAILED: dict[str, list[float]] = {}
WINDOW_SECONDS = 15 * 60
MAX_FAILED = 6
LOGIN = """<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GMGN Trader • Entrar</title><style>body{background:#0b1120;color:#eef2ff;font:16px system-ui;display:grid;place-items:center;min-height:95vh}form{display:grid;gap:16px;padding:30px;background:#172033;border-radius:16px;width:min(85vw,350px)}input,button{font:inherit;padding:12px;border-radius:8px}button{background:#38bdf8;border:0;cursor:pointer}</style><form method="post" action="/login"><h2>GMGN Trader</h2><p>Acesso pessoal • Simulação</p><input type="password" name="password" aria-label="Senha" placeholder="Senha" required autocomplete="current-password" maxlength="256"><button>Entrar</button></form></html>"""

@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # Browsers send this header; refuse explicit cross-origin submissions.
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return JSONResponse({"error": "cross-site blocked"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    return response

def authorized(request: Request) -> bool:
    return request.session.get("auth") is True

@app.get("/health")
def health():
    return {"status": "online", "service": "gmgn-trader", "trading_enabled": False}

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if authorized(request):
        return RedirectResponse("/", status_code=303)
    return LOGIN

@app.post("/login")
async def login(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = FAILED.get(ip, [])
    attempts = [t for t in attempts if now - t < WINDOW_SECONDS]
    FAILED[ip] = attempts
    if len(attempts) >= MAX_FAILED:
        return HTMLResponse("Muitas tentativas. Tente novamente mais tarde.", status_code=429, headers={"Retry-After": "900"})
    if int(request.headers.get("content-length") or "0") > 4096:
        return HTMLResponse("Solicitação muito grande.", status_code=413)
    raw = (await request.body())[:4096].decode("utf-8", "replace")
    password = parse_qs(raw, keep_blank_values=True).get("password", [""])[0]
    if secrets.compare_digest(password, PASSWORD):
        FAILED.pop(ip, None)
        request.session.clear()
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    attempts.append(now)
    FAILED[ip] = attempts
    return HTMLResponse(LOGIN.replace("<p>Acesso pessoal • Simulação</p>", "<p>Senha incorreta</p>"), status_code=401)

@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not authorized(request):
        return RedirectResponse("/login", status_code=303)
    return (BASE / "dashboard.html").read_text(encoding="utf-8")

@app.get("/api/status")
def status(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {
        "mode": "paper",
        "connected_to_gmgn": False,
        "trading_enabled": False,
        "networks": ["sol", "eth"],
        "planned_risk_pct": 2,
        "initial_paper_capital_usd": 100,
        "positions": [],
        "trades": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
