from __future__ import annotations

import json
import logging
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, field_validator

import conditions
import config
import db
import engine

# --- logging (console + rotating file) ---
logger = logging.getLogger("rsi_alerts")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
for _h in (logging.StreamHandler(), RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000, backupCount=3)):
    _h.setFormatter(_fmt)
    logger.addHandler(_h)


# --- request model (validates untrusted rule payloads at the HTTP boundary) ---
class RuleIn(BaseModel):
    name: str
    symbol: str
    timeframe: str
    condition: dict
    channels: list[str] = []
    recipients: list[str] = []
    enabled: bool = True
    cooldown_sec: int = 0

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v):
        v = v.upper().strip()
        if not v:
            raise ValueError("symbol required")
        return v

    @field_validator("timeframe")
    @classmethod
    def _timeframe(cls, v):
        if v not in config.TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {list(config.TIMEFRAMES)}")
        return v

    @field_validator("channels")
    @classmethod
    def _channels(cls, v):
        allowed = {"email"}  # SMS/WhatsApp/RCS retired
        bad = [c for c in v if c not in allowed]
        if bad:
            raise ValueError(f"channels must be a subset of {sorted(allowed)}; got {bad}")
        return v

    @field_validator("condition")
    @classmethod
    def _condition(cls, v):
        if not isinstance(v, dict) or "type" not in v:
            raise ValueError("condition must be an object with a 'type'")
        conditions.validate(v)  # raises ValueError on unknown type / missing params
        return v


class SettingsIn(BaseModel):
    email_recipients: list[str] = []

    @field_validator("email_recipients")
    @classmethod
    def _emails(cls, v):
        out = []
        for e in v:
            e = e.strip()
            if not e:
                continue
            if "@" not in e or "." not in e.rsplit("@", 1)[-1]:
                raise ValueError(f"invalid email: {e}")
            out.append(e)
        return out


class BacktestIn(BaseModel):
    symbol: str
    timeframe: str
    condition: dict

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v):
        v = v.upper().strip()
        if not v:
            raise ValueError("symbol required")
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
        conditions.validate(v)
        return v


def _seed_defaults():
    if db.list_rules():
        return
    logger.info("seeding default RSI rules")
    for sym in config.SYMBOLS:
        for tf in ("5min", "15min"):
            for direction, thr, tag in (("below", config.OVERSOLD, "oversold"),
                                        ("above", config.OVERBOUGHT, "overbought")):
                db.create_rule({
                    "id": uuid.uuid4().hex[:12], "name": f"{sym} {tf} {tag}",
                    "symbol": sym, "timeframe": tf,
                    "condition": {"type": "rsi", "period": 14, "threshold": thr, "direction": direction},
                    "channels": [], "recipients": [], "enabled": True, "cooldown_sec": 0,
                })


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    _seed_defaults()
    if "--demo" in sys.argv:
        logger.info("starting DEMO engine")
        engine.DEMO = True
        threading.Thread(target=engine.run_demo, daemon=True).start()
    else:
        engine.start_thread()
    yield


app = FastAPI(title="Market Alert System", lifespan=lifespan)

# allow the dashboard to call the API when opened as a local file / other origin
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def index():
    return FileResponse(config.ROOT / "index.html")


@app.get("/api/meta")
def meta():
    return {
        "symbols": config.SYMBOLS,
        "timeframes": [{"value": k, "label": v[2]} for k, v in config.TIMEFRAMES.items()],
        "channels": ["email"],
        "retired_channels": ["sms", "whatsapp", "rcs"],
        "condition_types": conditions.schema(),
        "dry_run": config.DRY_RUN,
        "email_enabled": bool(config.RESEND_API_KEY),
    }


@app.get("/api/snapshot")
def snapshot():
    if config.SNAPSHOT_FILE.exists():
        return JSONResponse(content=json.loads(config.SNAPSHOT_FILE.read_text()))
    return {"updated": None, "market_open": engine.market_is_open(), "dry_run": config.DRY_RUN, "rules": [], "alerts": []}


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
def alerts(limit: int = 50):
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


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        config.DRY_RUN = True
    logger.info("Market Alert System starting on :%d (dry_run=%s, demo=%s)",
                config.DASHBOARD_PORT, config.DRY_RUN, "--demo" in sys.argv)
    uvicorn.run(app, host="0.0.0.0", port=config.DASHBOARD_PORT, log_level="warning")
