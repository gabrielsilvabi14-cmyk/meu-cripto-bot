"""GMGN Trader: private paper-trading dashboard scaffold (no trading)."""
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

PASSWORD = os.getenv("TRADER_PASSWORD", "")
SECRET = os.getenv("SESSION_SECRET", "")
if not PASSWORD or len(SECRET) < 32:
    raise RuntimeError("Configure TRADER_PASSWORD and SESSION_SECRET (32+ chars) before starting")

app = FastAPI(title="GMGN Trader 1.0", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET, https_only=os.getenv("COOKIE_SECURE", "true").lower() == "true", same_site="strict", max_age=3600)
BASE = Path(__file__).resolve().parent
LOGIN = """<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GMGN Trader • Entrar</title><style>body{background:#0b1120;color:#eef2ff;font:16px system-ui;display:grid;place-items:center;min-height:95vh}form{display:grid;gap:16px;padding:30px;background:#172033;border-radius:16px;width:min(85vw,350px)}input,button{font:inherit;padding:12px;border-radius:8px}button{background:#38bdf8;border:0;cursor:pointer}</style><form method="post" action="/login"><h2>GMGN Trader</h2><p>Acesso pessoal • Simulação</p><input type="password" name="password" aria-label="Senha" placeholder="Senha" required autocomplete="current-password"><button>Entrar</button></form></html>"""

def authorized(request: Request) -> bool:
    return request.session.get("auth") is True

@app.get("/health")
def health():
    return {"status": "online", "service": "gmgn-trader", "trading_enabled": False}

@app.get("/login", response_class=HTMLResponse)
def login_page():
    return LOGIN

@app.post("/login")
async def login(request: Request):
    # Read URL-encoded form without optional multipart dependencies.
    from urllib.parse import parse_qs
    raw = (await request.body()).decode("utf-8", "replace")[:4096]
    password = parse_qs(raw).get("password", [""])[0]
    if secrets.compare_digest(password, PASSWORD):
        request.session.clear()
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
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
    return {"mode": "paper", "connected_to_gmgn": False, "trading_enabled": False, "networks": ["sol", "eth"], "planned_risk_pct": 2, "initial_paper_capital_usd": 100, "positions": [], "trades": [], "timestamp": datetime.now(timezone.utc).isoformat()}
