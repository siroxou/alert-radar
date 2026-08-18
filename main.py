from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from urllib.parse import quote

import requests
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from pydantic import BaseModel, ConfigDict, Field, field_validator

import ai
import alpaca_client
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
        # the symbol reaches an upstream URL path with our credentials attached,
        # so validate against whichever provider is actually going to be called
        if not engine.data_client().SYMBOL_RE.fullmatch(v):
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


def _seed_defaults(user_id, email):
    """Starter rules for a brand-new account.

    TWO rules, not the old global 24. The extra market data would be free — the
    seed pairs all share the cycle cache — but the email is not: that 24-rule set
    produced ~10k alerts a month in testing, which would exhaust a free Resend
    tier on one user and read as spam from an account they just signed up for.
    """
    if email:
        db.set_user_recipients(user_id, [email])  # their own Google address, by default
    logger.info("seeding starter rules for %s", user_id)
    for direction, thr, tag in (("below", config.OVERSOLD, "oversold"),
                                ("above", config.OVERBOUGHT, "overbought")):
        db.create_rule({
            "id": uuid.uuid4().hex[:12], "name": f"SPY 15min {tag}",
            "symbol": "SPY", "timeframe": "15min",
            "condition": {"type": "rsi", "period": 14, "threshold": thr, "direction": direction},
            "enabled": True,
        }, user_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    if config.SERVERLESS:
        logger.info("serverless mode — evaluation is driven by /api/cron/evaluate")
    elif "--demo" in sys.argv:
        logger.info("starting DEMO engine")
        engine.DEMO = True
        import threading
        threading.Thread(target=engine.run_demo, daemon=True).start()
    elif config.DATA_PROVIDER == "alpaca" and alpaca_client.available():
        # Resident process + websocket: alerts land seconds after a bar closes
        # instead of up to a poll interval later. A function cannot do this —
        # there is no process to hold the socket open.
        import stream
        logger.info("starting Alpaca stream engine (real-time %s)", config.ALPACA_FEED)
        stream.start_thread()
    else:
        engine.start_thread()  # REST polling every REFRESH_SECONDS
    yield


app = FastAPI(title="Market Alert System", lifespan=lifespan)

# The dashboard authenticates by cookie, and cookies require an explicit origin
# list. Without any auth configured (local dev) keep the permissive behaviour so
# index.html still works opened as a file. Note the `or SUPABASE_URL`: gating this
# on AUTH_TOKEN alone would silently fall back to allow_origins=["*"] the moment
# the shared token was dropped in favour of Google sign-in.
if config.AUTH_TOKEN or config.SUPABASE_URL:
    _origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    if config.SITE_URL:
        _origins.append(config.SITE_URL)
    if config.SERVERLESS and os.environ.get("VERCEL_URL"):
        _origins.append(f"https://{os.environ['VERCEL_URL']}")
    app.add_middleware(CORSMiddleware, allow_origins=_origins or ["https://localhost"],
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
else:
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# --- auth ------------------------------------------------------------------
# Google sign-in via Supabase, server-side PKCE. FastAPI performs the code
# exchange and mints its OWN HMAC-signed httpOnly cookie; the Supabase tokens are
# read once and thrown away. Nothing readable by JavaScript ever holds a session,
# which matters because the dashboard loads a third-party CDN script — the
# supabase-js alternative parks its JWT in localStorage, in reach of that script.
# The cron secret stays separate: a signed-in user must not be able to force
# evaluation cycles, each of which spends market-data quota and can send mail.

_OPEN_PATHS = {"/api/auth/start", "/api/auth/callback", "/api/logout", "/healthz"}


# --- stateless session cookie ---
# Serverless is a fresh process per request, so there is nowhere to keep a session
# store: the signature IS the store.
# ponytail: no revocation list — rotating SESSION_SECRET logs everyone out.

def _b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _b64d(s):
    return base64.urlsafe_b64decode(s + b"=" * (-len(s) % 4))


def _mac(body):
    return _b64e(hmac.new(config.SESSION_SECRET.encode(), body, hashlib.sha256).digest())


def make_session(uid, email, days=30):
    body = _b64e(json.dumps({"sub": uid, "email": email, "exp": int(time.time()) + days * 86400},
                            separators=(",", ":")).encode())
    return (body + b"." + _mac(body)).decode()


def read_session(cookie):
    try:
        body, sig = cookie.encode().split(b".", 1)
        if not hmac.compare_digest(sig, _mac(body)):
            return None
        data = json.loads(_b64d(body))
    except Exception:
        return None  # malformed is indistinguishable from forged, deliberately
    return data if data.get("exp", 0) > time.time() and data.get("sub") else None


def _user(request: Request):
    """The caller's identity, or None. Every branch yields a real, non-null id."""
    if config.SUPABASE_URL and config.SESSION_SECRET:
        s = read_session(request.cookies.get(config.SESSION_COOKIE, ""))
        if s:
            return s
    header = request.headers.get("authorization", "")
    if config.AUTH_TOKEN and header.startswith("Bearer ") and hmac.compare_digest(header[7:], config.AUTH_TOKEN):
        # ponytail: the operator's own token, mapped to a fixed id rather than a
        # "sees everything" sentinel — one forgotten filter should not leak a
        # stranger's rules. Set OPERATOR_USER_ID to your own Supabase uid.
        return {"sub": config.OPERATOR_USER_ID, "email": ""}
    if not config.SUPABASE_URL and not config.AUTH_TOKEN:
        return {"sub": config.LOCAL_USER_ID, "email": ""}  # local dev; refused outright when deployed
    return None


def me(request: Request):
    """FastAPI dependency: the signed-in user. The gate has already run."""
    return _user(request) or {"sub": config.LOCAL_USER_ID, "email": ""}


@app.middleware("http")
async def _gate(request: Request, call_next):
    path = request.url.path
    # FIRST, above the fail-closed check: the cron endpoint carries its own secret
    # and must keep running even when dashboard auth is misconfigured. A broken
    # login should not silently stop the monitor.
    if path == "/api/cron/evaluate":
        return await call_next(request)
    if path in _OPEN_PATHS or request.method == "OPTIONS":
        return await call_next(request)
    # Refuse to serve a public deployment with no auth at all, rather than exposing
    # rule creation and the alert recipient list to the internet.
    if config.SERVERLESS and not (config.SUPABASE_URL and config.SESSION_SECRET) and not config.AUTH_TOKEN:
        return JSONResponse(status_code=503, content={
            "detail": "Auth is not configured. Set SUPABASE_URL + SESSION_SECRET "
                      "(or ALERT_RADAR_TOKEN) in the project's environment variables."})
    if _user(request):
        return await call_next(request)
    if path == "/" or path.startswith("/docs") or path.startswith("/redoc"):
        return HTMLResponse(_login_page(), status_code=401)
    return JSONResponse(status_code=401, content={"detail": "Not authenticated"})


def _login_page(msg=""):
    """`msg` only ever receives fixed literals from this module. Never reflect a
    provider-supplied string (error_description, any query param) into it — this
    is the one page that ships without a CSP."""
    return f"""<!doctype html><meta charset=utf-8><title>Alert Radar</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{{font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#f2edfc;color:#2d2a45;
display:grid;place-items:center;min-height:100vh;margin:0}}main{{background:#fff;padding:32px;border-radius:20px;
box-shadow:0 8px 24px rgba(97,80,235,.14);max-width:340px;width:90%;text-align:center}}
h1{{font-size:20px;margin:0 0 4px}}p{{color:#67628a;font-size:14px;margin:0 0 20px}}
a.btn{{display:flex;align-items:center;justify-content:center;gap:10px;padding:12px;border-radius:10px;
background:#5646e5;color:#fff;font-weight:700;font-size:15px;text-decoration:none}}
.err{{color:#a62b47;font-size:13px;margin:12px 0 0;min-height:18px}}</style>
<main><h1>&#128680; Alert Radar</h1><p>Sign in to manage your alerts.</p>
<a class=btn href="/api/auth/start">Sign in with Google</a>
<p class=err>{msg}</p></main>"""


@app.get("/api/auth/start")
def auth_start():
    if not (config.SUPABASE_URL and config.SITE_URL and config.SESSION_SECRET):
        raise HTTPException(status_code=503, detail="Google sign-in is not configured.")
    verifier = secrets.token_urlsafe(64)  # RFC 7636 wants 43-128 chars
    challenge = _b64e(hashlib.sha256(verifier.encode()).digest()).decode()
    redirect_to = f"{config.SITE_URL}/api/auth/callback"
    url = (f"{config.SUPABASE_URL}/auth/v1/authorize?provider=google"
           f"&redirect_to={quote(redirect_to, safe='')}"
           f"&code_challenge={challenge}&code_challenge_method=s256")
    r = RedirectResponse(url, status_code=302)
    # The verifier cookie doubles as the CSRF defence: a login-CSRF would need the
    # attacker's code to match the victim's cookie-held verifier, which it cannot.
    r.set_cookie(config.PKCE_COOKIE, verifier, httponly=True, samesite="lax",
                 secure=config.SERVERLESS, max_age=600, path="/api/auth")
    return r


@app.get("/api/auth/callback")
def auth_callback(request: Request, code: str = ""):
    verifier = request.cookies.get(config.PKCE_COOKIE, "")
    if not code or not verifier:
        return HTMLResponse(_login_page("Sign-in was cancelled or timed out."), status_code=401)
    r = requests.post(f"{config.SUPABASE_URL}/auth/v1/token?grant_type=pkce", timeout=15,
                      headers={"apikey": config.SUPABASE_ANON_KEY, "Content-Type": "application/json"},
                      json={"auth_code": code, "code_verifier": verifier})
    if r.status_code != 200:
        logger.warning("pkce exchange failed: %s %s", r.status_code, r.text[:200])
        return HTMLResponse(_login_page("Sign-in failed. Please try again."), status_code=401)
    # The token response carries the user, so there is no second call and no JWT
    # signature to verify — which is what keeps this dependency-free. Both Supabase
    # tokens are read here and discarded; they never leave this function.
    u = (r.json() or {}).get("user") or {}
    uid, email = u.get("id"), (u.get("email") or "")
    if not uid:
        return HTMLResponse(_login_page("Sign-in failed."), status_code=401)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(config.SESSION_COOKIE, make_session(uid, email), httponly=True, samesite="lax",
                    secure=config.SERVERLESS, max_age=60 * 60 * 24 * 30, path="/")
    resp.delete_cookie(config.PKCE_COOKIE, path="/api/auth")
    return resp


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
def meta(user: dict = Depends(me)):
    return {
        "symbols": config.SYMBOLS,
        "timeframes": [{"value": k, "label": v[2]} for k, v in config.TIMEFRAMES.items()],
        "condition_types": conditions.schema(),
        "dry_run": config.DRY_RUN,
        "email_enabled": bool(config.RESEND_API_KEY),
        "ai_enabled": config.AI_ENABLED and ai.available(),
        "auth_enabled": bool(config.SUPABASE_URL or config.AUTH_TOKEN),
        "user": user.get("email") or "",
        "max_rules": config.MAX_RULES_PER_USER,
        # Served rather than hardcoded in the UI: the Settings panel named the
        # wrong provider for two releases because it restated a server fact the
        # client had no way to read.
        "data_provider": config.DATA_PROVIDER,
        "data_feed": config.ALPACA_FEED if config.DATA_PROVIDER == "alpaca" else "",
        "serverless": config.SERVERLESS,
    }


@app.get("/api/snapshot")
def snapshot(user: dict = Depends(me)):
    return engine.snapshot(user["sub"])


@app.get("/api/rules")
def get_rules(user: dict = Depends(me)):
    rules = db.list_rules(user["sub"])
    # Seed lazily, on first sight of an empty account, rather than in lifespan —
    # which has no user. claim_once keeps two cold starts from double-seeding, and
    # because the claim is consumed, a deliberate delete-all is never re-seeded.
    if not rules and db.claim_once(db.ukey(user["sub"], "seeded")):
        _seed_defaults(user["sub"], user.get("email") or "")
        rules = db.list_rules(user["sub"])
    return rules


@app.post("/api/rules")
def create_rule(rule: RuleIn, user: dict = Depends(me)):
    # Open signup on a paid market-data key: bound the spend per account. A budget
    # guard, not a security boundary — two concurrent POSTs may both pass, and
    # landing at 26 rules is fine.
    if len(db.list_rules(user["sub"])) >= config.MAX_RULES_PER_USER:
        raise HTTPException(status_code=403,
                            detail=f"Rule limit reached ({config.MAX_RULES_PER_USER}). Delete one first.")
    data = rule.model_dump()
    data["id"] = uuid.uuid4().hex[:12]
    return db.create_rule(data, user["sub"])


@app.put("/api/rules/{rid}")
def update_rule(rid: str, rule: RuleIn, user: dict = Depends(me)):
    # 404 rather than 403 on someone else's id: don't confirm that it exists.
    if not db.get_rule(rid, user["sub"]):
        raise HTTPException(status_code=404, detail="rule not found")
    return db.update_rule(rid, rule.model_dump(), user["sub"])


@app.delete("/api/rules/{rid}")
def delete_rule(rid: str, user: dict = Depends(me)):
    if not db.get_rule(rid, user["sub"]):
        raise HTTPException(status_code=404, detail="rule not found")
    db.delete_rule(rid, user["sub"])
    # Nothing overwrites a user's snapshot blob when they have no rules left, so
    # the deleted ones would linger on the dashboard. Partial deletes self-heal on
    # the next cycle; only the empty case needs clearing.
    if not db.list_rules(user["sub"]):
        db.set_setting(db.ukey(user["sub"], "snapshot"), "")
    return {"deleted": rid}


@app.get("/api/alerts")
def alerts(limit: int = Query(50, ge=1, le=500), user: dict = Depends(me)):
    return db.list_alerts(user["sub"], limit)


@app.get("/api/settings")
def get_settings(user: dict = Depends(me)):
    return {"email_recipients": db.user_recipients(user["sub"])}


@app.put("/api/settings")
def put_settings(s: SettingsIn, user: dict = Depends(me)):
    db.set_user_recipients(user["sub"], s.email_recipients)
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
    started = time.monotonic()
    aligned_to = _align_to_boundary()
    engine.run_cycle()
    return {"ok": True, "market_open": engine.market_is_open(), "at": engine.now_et(),
            "aligned_to": aligned_to, "took_ms": int((time.monotonic() - started) * 1000)}


def _align_to_boundary():
    """Sleep until just after the next bar close, when new data can actually exist.

    Rules only ever see CLOSED bars, so evaluating at an arbitrary offset is wasted
    work — and worse, a ping landing seconds BEFORE a boundary reads the stale bar
    and the next one is a full interval away. Waiting the few seconds for the
    boundary turns "up to a whole interval late" into "publish lag plus a cycle".

    Returns the ISO boundary it waited for, or None if it did not wait.
    """
    if not config.BOUNDARY_ALIGN or engine.DEMO:
        return None
    try:
        # One extra lightweight read before the cycle. It pays for itself: aligning
        # to the rules' ACTUAL timeframes is what lets ~14 of every 15 pings skip
        # sleeping entirely when all rules are 15min. Aligning to the shortest
        # configured timeframe instead would need no query and sleep every minute.
        timeframes = {r["timeframe"] for r in db.all_enabled_rules()}
        boundary = engine.previous_boundary(timeframes)
        if boundary is None:
            return None
        # The bar that JUST closed is the one worth waiting for — a ping at 10:00:04
        # is 14s from the 10:00 bar publishing. Waiting for the NEXT boundary instead
        # would add a whole interval to this invocation for nothing; the next ping
        # covers that bar.
        ready = boundary + timedelta(seconds=config.BAR_PUBLISH_LAG_SECONDS)
        wait = (ready - datetime.now(engine.ET)).total_seconds()
        # Already published (evaluate now, the data is fresh), or further off than we
        # will hold the invocation open for.
        if wait <= 0 or wait > config.MAX_ALIGN_SLEEP_SECONDS:
            return None
        # One evaluation per boundary: overlapping pings would otherwise both wake
        # up and duplicate every market-data fetch. The edge claim already prevents
        # duplicate EMAILS; this prevents duplicate WORK.
        if not db.claim_once(f"cycle:{int(boundary.timestamp() // 60)}"):
            return None
        logger.info("aligning: sleeping %.1fs for the %s bar close", wait, boundary.strftime("%H:%M"))
        time.sleep(wait)
        return boundary.isoformat()
    except Exception:
        logger.exception("boundary alignment failed; evaluating immediately")
        return None


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


# ponytail: single-slot TTL keyed on (user, newest alert id) — the user is part of
# the key so one person's summary can never be served to another.
_ai_summary_cache = {"key": None, "text": None}


@app.get("/api/ai/insights-summary")
def ai_insights_summary(user: dict = Depends(me)):
    alerts = db.list_alerts(user["sub"], 50)
    if not alerts:
        return {"summary": "No alerts have fired yet — your summary appears once rules start triggering.", "cached": False}
    key = (user["sub"], alerts[0]["id"])  # regenerate only when a new alert lands
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
