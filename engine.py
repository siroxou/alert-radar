"""Evaluation loop: for each enabled rule → fetch bars → evaluate condition →
dedup transition → notify (sent.dm/dry-run) → append history → write snapshot."""
import json
import logging
import os
import random
import threading
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import conditions
import config
import db
import notifier
import massive_client
import rsi as indicators

logger = logging.getLogger("rsi_alerts")
ET = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)

DEMO = False              # set True by main under --demo → watchlist/backtest use synthetic data
_last_success = time.time()  # heartbeat for the dead-man's switch
_deadman_fired = False
_last_error = ""


def market_is_open():
    now = datetime.now(ET)
    # ponytail: weekday + hours only, no exchange-holiday calendar.
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def now_et():
    return datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")


def _fetch_series(symbol, timeframe):
    mult, span, _disp, lookback = config.TIMEFRAMES[timeframe]
    closes, volumes = massive_client.get_bars(symbol, mult, span, lookback)
    return {"closes": closes, "volumes": volumes}


def _provider(series_provider=None):
    """Series source for on-demand endpoints: caller override → demo → live Massive."""
    return series_provider or (demo_series if DEMO else _fetch_series)


def run_once(series_provider=None):
    provider = series_provider or _fetch_series
    rules = db.list_rules(enabled_only=True)
    cache = {}  # (symbol,timeframe) -> series, so each pair is fetched once per cycle
    rows = []
    for rule in rules:
        key = (rule["symbol"], rule["timeframe"])
        try:
            if key not in cache:
                cache[key] = provider(*key)
            series = cache[key]
            fired, value = conditions.evaluate(rule["condition"], series)
            price = series["closes"][-1]
            desc = conditions.describe(rule["condition"])
            if db.record_transition(rule["id"], fired, value):
                notifier.send(rule, desc, value, price, now_et())
                db.append_alert(rule, desc, value, price)
                logger.info("ALERT: %s [%s] value=%.2f price=%.2f", rule["name"], desc, value, price)
            rows.append(_row(rule, desc, value, fired))
        except Exception:
            logger.exception("error evaluating rule %s (%s)", rule.get("id"), rule.get("name"))
            rows.append(_row(rule, "error", None, False, error=True))
    global _last_error
    ok = (not rows) or any(not r.get("error") for r in rows)
    if ok:
        _mark_success()
    elif rows:
        _last_error = "all rules failed to evaluate (market data unavailable?)"
    _write_snapshot(rows)


def _row(rule, desc, value, fired, error=False):
    return {
        "id": rule["id"], "name": rule["name"], "symbol": rule["symbol"],
        "timeframe": rule["timeframe"], "type": rule["condition"].get("type"),
        "description": desc, "value": None if value is None else round(value, 2),
        "fired": bool(fired), "enabled": True, "error": error,
    }


def _write_snapshot(rows):
    stale = time.time() - _last_success
    data = json.dumps({
        "updated": now_et(),
        "market_open": market_is_open(),
        "dry_run": config.DRY_RUN,
        "last_success_ms": int(_last_success * 1000),
        "healthy": stale <= config.DEADMAN_SECONDS,
        "deadman_seconds": config.DEADMAN_SECONDS,
        "rules": rows,
        "alerts": db.list_alerts(50),
    }, indent=2)
    # atomic write so a concurrent /api/snapshot read never sees a half-written file
    tmp = config.SNAPSHOT_FILE.parent / (config.SNAPSHOT_FILE.name + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, config.SNAPSHOT_FILE)


def _mark_success():
    global _last_success
    _last_success = time.time()


def _check_deadman():
    """Fire the dead-man's switch once when the loop stops completing healthy cycles."""
    global _deadman_fired
    stale = time.time() - _last_success
    if stale > config.DEADMAN_SECONDS:
        if not _deadman_fired:
            logger.error("DEAD-MAN tripped: %.0fs since last healthy cycle", stale)
            try:
                notifier.send_deadman(_last_error or "no successful cycle", stale)
            except Exception:
                logger.exception("dead-man notify failed")
            _deadman_fired = True
    else:
        _deadman_fired = False


def run_loop():
    logger.info("engine loop starting (refresh=%ss, deadman=%ss)", config.REFRESH_SECONDS, config.DEADMAN_SECONDS)
    global _last_error
    while True:
        try:
            if market_is_open():
                run_once()
            else:
                # keep the dashboard's rule list fresh, no evaluation while closed
                rows = [_row(r, conditions.describe(r["condition"]), None, False)
                        for r in db.list_rules(enabled_only=True)]
                _mark_success()  # closed market is a healthy idle, not a failure
                _write_snapshot(rows)
                logger.info("market closed, idling")
        except Exception as exc:
            _last_error = repr(exc)
            logger.exception("cycle error")
        _check_deadman()
        time.sleep(config.REFRESH_SECONDS)


def start_thread():
    threading.Thread(target=run_loop, daemon=True).start()


# --- Demo mode: synthetic random-walk series with drifting trends so conditions ---
# --- actually cross thresholds (and reset), exercising the real pipeline offline. ---
_demo_closes = {}
_demo_drift = {}
_DEMO_BASE = {"AAPL": 241.0, "NVDA": 188.0, "AMZN": 220.0, "IWM": 231.0, "QQQ": 561.0, "SPY": 622.0}


def demo_series(symbol, timeframe):
    key = f"{symbol}_{timeframe}"
    closes = _demo_closes.get(key)
    if closes is None:
        base = _DEMO_BASE.get(symbol, 100.0)
        d0 = random.uniform(-0.011, 0.011)  # baked-in trend → varied (incl. extreme) starting RSI
        _demo_drift[key] = d0
        closes = [base]
        for _ in range(120):
            closes.append(max(1.0, closes[-1] * (1 + d0 + random.uniform(-0.004, 0.004))))
    else:
        drift = _demo_drift.get(key, 0.0)
        if random.random() < 0.15:  # occasionally start/flip a trend → pushes RSI to extremes
            drift = random.uniform(-0.012, 0.012)
        _demo_drift[key] = drift
        closes.append(max(1.0, closes[-1] * (1 + drift + random.uniform(-0.005, 0.005))))
        if len(closes) > 250:
            closes = closes[-250:]
    _demo_closes[key] = closes
    return {"closes": list(closes), "volumes": [1_000_000.0] * len(closes)}


def run_demo():
    logger.info("engine DEMO mode — synthetic data, notifications dry-run")
    while True:
        try:
            run_once(series_provider=demo_series)
        except Exception:
            logger.exception("demo cycle error")
        time.sleep(2.5)


# --- on-demand endpoints (Watchlist / Backtest) ---
_wl_cache = {"ts": 0.0, "data": None}


def watchlist(series_provider=None, ttl=20):
    """Live price + RSI for every watched symbol × timeframe (the Watchlist grid)."""
    provider = _provider(series_provider)
    now = time.time()
    if series_provider is None and _wl_cache["data"] is not None and now - _wl_cache["ts"] < ttl:
        return _wl_cache["data"]  # ponytail: 20s TTL so page polls don't refetch 24 series each time
    out = []
    for sym in config.SYMBOLS:
        price, rows = None, []
        for tf in config.TIMEFRAMES:
            try:
                closes = provider(sym, tf)["closes"]
                price = closes[-1]
                rows.append({"timeframe": tf, "rsi": round(indicators.wilder_rsi(closes, config.RSI_PERIOD), 1)})
            except Exception:
                rows.append({"timeframe": tf, "rsi": None})
        out.append({"symbol": sym, "price": None if price is None else round(price, 2), "rows": rows})
    if series_provider is None:
        _wl_cache.update(ts=now, data=out)
    return out


def backtest(symbol, timeframe, condition, series_provider=None):
    """Replay a condition bar-by-bar over the lookback window; count fresh False→True fires."""
    provider = _provider(series_provider)
    series = provider(symbol, timeframe)
    closes, volumes = series["closes"], series["volumes"]
    fires, prev = [], False
    for i in range(max(conditions.min_bars(condition), 2), len(closes) + 1):
        try:
            fired, value = conditions.evaluate(condition, {"closes": closes[:i], "volumes": volumes[:i]})
        except Exception:
            continue  # not enough bars yet for this indicator
        if fired and not prev:
            fires.append({"index": i - 1, "value": round(value, 2), "price": round(closes[i - 1], 2)})
        prev = fired
    return {
        "symbol": symbol, "timeframe": timeframe, "description": conditions.describe(condition),
        "bars": len(closes), "fires": len(fires), "events": fires[-50:],
    }
