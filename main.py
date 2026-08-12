from __future__ import annotations

import hmac
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

import ai
import conditions
import config
import db
import engine
import massive_client

# --- logging ---------------------------------------------------------------
# stdout always; a rotating file only where a writable disk exists. On a
# read-only serverless filesystem, constructing the handler at import time is
# itself the crash.
logger = logging.getLogger("rsi_alerts")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_handlers = [logging.StreamHandler()]
if not config.SERVERLESS:
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _handlers.append(RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000, backupCount=3))
for _h in _handlers:
    _h.setFormatter(_fmt)
    logger.addHandler(_h)


# --- request models (validate untrusted payloads at the HTTP boundary) ------

class _RuleFields(BaseModel):
    """Fields shared by rule creation and backtesting, validated identically."""
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: str
    condition: dict

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v):
        v = v.upper().strip()
        # the symbol reaches an upstream URL path with our API key attached
        if not massive_client.SYMBOL_RE.fullmatch(v):
            raise ValueError("symbol must be 1-10 characters of A-Z, '.' or '-'")
        return v

    @field_validator("timeframe")
    @classmethod
    def _timeframe(cls, v):
        if v not in config.TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {list(config.TIMEFRAMES)}")
        return v

    @field_validator("condition")
    @classmethod
    def _condition(cls, v):
        if not isinstance(v, dict) or "type" not in v:
            raise ValueError("condition must be an object with a 'type'")
        conditions.validate(v)  # raises ValueError on unknown type / missing params
        return v


class RuleIn(_RuleFields):
    name: str
    enabled: bool = True


class BacktestIn(_RuleFields):
    pass


class SettingsIn(BaseModel):
    email_recipients: list = []

    @field_validator("email_recipients")
    @classmethod
    def _emails(cls, v):
        out = []
        for e in v:
            e = str(e).strip()
            if not e:
                continue
            if "@" not in e or "." not in e.rsplit("@", 1)[-1]:
                raise ValueError(f"invalid email: {e}")
            out.append(e)
        return out


class AITextIn(BaseModel):
    # bounded: this is one sentence describing an alert, and it becomes LLM spend
    text: str = Field(max_length=500)


class LoginIn(BaseModel):
    token: str = Field(max_length=200)


def _seed_defaults():
    if db.list_rules() or not db.claim_once("seeded"):
        return  # claim_once is atomic, so two cold starts cannot both seed
    logger.info("seeding default RSI rules")
    for sym in config.SYMBOLS:
        for tf in ("5min", "15min"):
            for direction, thr, tag in (("below", config.OVERSOLD, "oversold"),
                                        ("above", config.OVERBOUGHT, "overbought")):
                db.create_rule({
                    "id": uuid.uuid4().hex[:12], "name": f"{sym} {tf} {tag}",
                    "symbol": sym, "timeframe": tf,
                    "condition": {"type": "rsi", "period": 14, "threshold": thr, "direction": direction},
                    "enabled": True,
                })


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    _seed_defaults()
    if config.SERVERLESS:
        logger.info("serverless mode — evaluation is driven by /api/cron/evaluate")
    elif "--demo" in sys.argv:
        logger.info("starting DEMO engine")
        engine.DEMO = True
        import threading
        threading.Thread(target=engine.run_demo, daemon=True).start()
    else:
        engine.start_thread()
    yield


app = FastAPI(title="Market Alert System", lifespan=lifespan)

# With a shared password the dashboard authenticates by cookie, and cookies
# require an explicit origin list. Without one (local dev) keep the old
# permissive behaviour so index.html still works opened as a file.
if config.AUTH_TOKEN:
    _origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    if config.SERVERLESS and os.environ.get("VERCEL_URL"):
        _origins.append(f"https://{os.environ['VERCEL_URL']}")
    app.add_middleware(CORSMiddleware, allow_origins=_origins or ["https://localhost"],
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
else:
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# --- auth ------------------------------------------------------------------
# One shared password. The browser trades it for an httpOnly cookie, so the
# token is never reachable from JavaScript — which matters because the page
# loads a third-party CDN script. API clients may send a bearer header instead.
# The cron secret is deliberately separate: a dashboard user must not be able to
# force evaluation cycles.

_OPEN_PATHS = {"/api/login", "/api/logout", "/healthz"}


def _token_ok(supplied):
    return bool(supplied) and hmac.compare_digest(str(supplied), config.AUTH_TOKEN)


def _authed(request: Request):
    if not config.AUTH_TOKEN:
        return True  # no password configured → local, open (refused outright when deployed)
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and _token_ok(header[7:]):
        return True
    return _token_ok(request.cookies.get(config.SESSION_COOKIE))


@app.middleware("http")
async def _gate(request: Request, call_next):
    path = request.url.path
    if path in _OPEN_PATHS or request.method == "OPTIONS":
        return await call_next(request)
    # Refuse to serve a public deployment with no password at all, rather than
    # exposing rule creation and the alert recipient list to the internet.
    if config.SERVERLESS and not config.AUTH_TOKEN:
        return JSONResponse(status_code=503, content={
            "detail": "ALERT_RADAR_TOKEN is not set. Set it in the project's environment variables."})
    if path == "/api/cron/evaluate":
        return await call_next(request)  # authenticated by CRON_SECRET in the handler
    if _authed(request):
        return await call_next(request)
    if path == "/" or path.startswith("/docs") or path.startswith("/redoc"):
        return HTMLResponse(_LOGIN_PAGE, status_code=401)
    return JSONResponse(status_code=401, content={"detail": "Not authenticated"})


_LOGIN_PAGE = """<!doctype html><meta charset=utf-8><title>Alert Radar</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#f2edfc;color:#2d2a45;
display:grid;place-items:center;min-height:100vh;margin:0}form{background:#fff;padding:32px;border-radius:20px;
box-shadow:0 8px 24px rgba(97,80,235,.14);max-width:320px;width:90%}h1{font-size:20px;margin:0 0 4px}
p{color:#67628a;font-size:14px;margin:0 0 20px}input{width:100%;padding:12px;border:1px solid #e3ddf7;
border-radius:10px;font-size:15px;box-sizing:border-box}button{width:100%;margin-top:12px;padding:12px;
border:0;border-radius:10px;background:#5646e5;color:#fff;font-weight:700;font-size:15px;cursor:pointer}
.err{color:#a62b47;font-size:13px;margin-top:10px;min-height:18px}</style>
<form onsubmit="go(event)"><h1>&#128680; Alert Radar</h1><p>Enter your access token to continue.</p>
<input id=t type=password autofocus placeholder="Access token"><button>Unlock</button>
<div class=err id=e></div></form><script>
async function go(ev){ev.preventDefault();document.getElementById('e').textContent='';
const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
credentials:'same-origin',body:JSON.stringify({token:document.getElementById('t').value})});
if(r.ok){location.reload()}else{document.getElementById('e').textContent='Incorrect token.'}}
</script>"""


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    if not config.AUTH_TOKEN or not _token_ok(body.token):
        raise HTTPException(status_code=401, detail="Incorrect token")
    response.set_cookie(config.SESSION_COOKIE, body.token, httponly=True, samesite="lax",
                        secure=config.SERVERLESS, max_age=60 * 60 * 24 * 30, path="/")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(config.ROOT / "index.html")


@app.get("/api/meta")
def meta():
    return {
        "symbols": config.SYMBOLS,
        "timeframes": [{"value": k, "label": v[2]} for k, v in config.TIMEFRAMES.items()],
        "condition_types": conditions.schema(),
        "dry_run": config.DRY_RUN,
        "email_enabled": bool(config.RESEND_API_KEY),
        "ai_enabled": config.AI_ENABLED and ai.available(),
        "auth_enabled": bool(config.AUTH_TOKEN),
    }


@app.get("/api/snapshot")
def snapshot():
    return engine.snapshot()


@app.get("/api/rules")
def get_rules():
    return db.list_rules()


@app.post("/api/rules")
def create_rule(rule: RuleIn):
    data = rule.model_dump()
    data["id"] = uuid.uuid4().hex[:12]
    return db.create_rule(data)


@app.put("/api/rules/{rid}")
def update_rule(rid: str, rule: RuleIn):
    if not db.get_rule(rid):
        raise HTTPException(status_code=404, detail="rule not found")
    return db.update_rule(rid, rule.model_dump())


@app.delete("/api/rules/{rid}")
def delete_rule(rid: str):
    if not db.get_rule(rid):
        raise HTTPException(status_code=404, detail="rule not found")
    db.delete_rule(rid)
    return {"deleted": rid}


@app.get("/api/alerts")
def alerts(limit: int = Query(50, ge=1, le=500)):
    return db.list_alerts(limit)


@app.get("/api/settings")
def get_settings():
    saved = db.get_setting("email_recipients")
    recipients = [e.strip() for e in saved.split(",") if e.strip()] if saved is not None else config.DEFAULT_EMAIL_RECIPIENTS
    return {"email_recipients": recipients, "using_default": saved is None}


@app.put("/api/settings")
def put_settings(s: SettingsIn):
    db.set_setting("email_recipients", ",".join(s.email_recipients))
    return {"email_recipients": s.email_recipients}


@app.get("/api/watchlist")
def watchlist():
    return engine.watchlist()


@app.post("/api/backtest")
def backtest(bt: BacktestIn):
    return engine.backtest(bt.symbol, bt.timeframe, bt.condition)


@app.get("/api/cron/evaluate")
def cron_evaluate(request: Request):
    """One evaluation cycle, driven by Vercel Cron (or any external scheduler).

    Guarded by CRON_SECRET rather than the dashboard token, so a dashboard user
    cannot force cycles — each one spends market-data quota and can send mail.
    """
    if not config.CRON_SECRET:
        raise HTTPException(status_code=503, detail="CRON_SECRET is not set")
    header = request.headers.get("authorization", "")
    if not (header.startswith("Bearer ") and hmac.compare_digest(header[7:], config.CRON_SECRET)):
        raise HTTPException(status_code=401, detail="Not authenticated")
    engine.run_cycle()
    return {"ok": True, "market_open": engine.market_is_open(), "at": engine.now_et()}


@app.post("/api/ai/parse-rule")
def ai_parse_rule(body: AITextIn):
    """Plain English -> a rule draft (validated, not saved) for the New Alert form to pre-fill."""
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Describe the alert first.")
    try:
        draft = ai.parse_rule(text)
    except ai.AIUnavailable:
        raise HTTPException(status_code=503, detail="Local AI isn't available — fill the form in manually.")
    if draft.get("timeframe") not in config.TIMEFRAMES:
        raise HTTPException(status_code=422, detail="Couldn't pick a timeframe — try naming one of: " + ", ".join(config.TIMEFRAMES))
    try:
        conditions.validate(draft.get("condition") or {})  # same validation the manual form uses
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f'Couldn\'t turn that into a valid rule ({e}). Try e.g. "NVDA 5-min RSI below 15".')
    return {"name": draft.get("name") or "New alert", "symbol": draft["symbol"],
            "timeframe": draft["timeframe"], "condition": draft["condition"]}


_ai_summary_cache = {"key": None, "text": None}  # ponytail: single-slot TTL keyed on newest alert id


@app.get("/api/ai/insights-summary")
def ai_insights_summary():
    alerts = db.list_alerts(50)
    if not alerts:
        return {"summary": "No alerts have fired yet — your summary appears once rules start triggering.", "cached": False}
    key = alerts[0]["id"]  # regenerate only when a new alert lands
    if _ai_summary_cache["key"] == key and _ai_summary_cache["text"]:
        return {"summary": _ai_summary_cache["text"], "cached": True}
    by_symbol, by_hour = {}, {}
    for a in alerts:
        by_symbol[a["symbol"]] = by_symbol.get(a["symbol"], 0) + 1
        h = (a.get("ts") or "")[11:13]
        if h:
            by_hour[h] = by_hour.get(h, 0) + 1
    stats = {"total": len(alerts), "by_symbol": by_symbol, "by_hour": by_hour,
             "most_active": max(by_symbol, key=by_symbol.get)}
    try:
        text = ai.summarize_insights(stats)
    except ai.AIUnavailable:
        raise HTTPException(status_code=503, detail="Local AI isn't available right now.")
    _ai_summary_cache.update(key=key, text=text)
    return {"summary": text, "cached": False}


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        config.DRY_RUN = True
    logger.info("Market Alert System starting on :%d (dry_run=%s, demo=%s)",
                config.DASHBOARD_PORT, config.DRY_RUN, "--demo" in sys.argv)
    uvicorn.run(app, host="0.0.0.0", port=config.DASHBOARD_PORT, log_level="warning")
