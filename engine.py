"""Evaluation loop: for each enabled rule → fetch bars → evaluate condition →
dedup transition → record history → email → publish snapshot.

Two drivers, one cycle. An always-on host (Docker/systemd) runs run_loop();
a serverless host (Vercel) hits /api/cron/evaluate. Both call run_cycle(), so
the market-hours gate can never be present in one path and missing in the other.
"""
import json
import logging
import random
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import alpaca_client
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
EARLY_CLOSE = dtime(13, 0)

DEMO = False              # set True by main under --demo → watchlist/backtest use synthetic data
_last_error = ""


# --- NYSE calendar ---------------------------------------------------------
# Computed from the published rules rather than a hardcoded date table: a table
# silently rots the year it runs out, and this is the gate that decides whether
# we trade-check at all. Verified against NYSE's published 2026/2027 calendars
# in test_all.py.

def _easter(year):
    """Anonymous Gregorian computus — Good Friday is Easter minus two days."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month, day = divmod(h + ll - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year, month, weekday, n):
    """n-th `weekday` (Mon=0) of a month; n=-1 means the last one."""
    if n == -1:
        d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        return d
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(weeks=n - 1)


def _observed(d):
    """NYSE observance: Saturday holidays move to the preceding Friday, Sunday to the following Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def market_holidays(year):
    """Full-closure dates for a calendar year."""
    days = {
        _nth_weekday(year, 1, 0, 3),                    # MLK Jr. Day
        _nth_weekday(year, 2, 0, 3),                    # Washington's Birthday
        _easter(year) - timedelta(days=2),              # Good Friday
        _nth_weekday(year, 5, 0, -1),                   # Memorial Day
        _observed(date(year, 6, 19)),                   # Juneteenth
        _observed(date(year, 7, 4)),                    # Independence Day
        _nth_weekday(year, 9, 0, 1),                    # Labor Day
        _nth_weekday(year, 11, 3, 4),                   # Thanksgiving
        _observed(date(year, 12, 25)),                  # Christmas
    }
    # New Year's Day: when Jan 1 lands on a Saturday the NYSE observes nothing at
    # all — it does NOT close the preceding Friday, unlike every other holiday.
    if date(year, 1, 1).weekday() != 5:
        days.add(_observed(date(year, 1, 1)))
    return days


def early_close_days(year):
    """Dates closing at 13:00 ET instead of 16:00."""
    holidays = market_holidays(year)
    days = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}  # the Friday after Thanksgiving
    for d in (date(year, 7, 3), date(year, 12, 24)):
        # a half-day only if it is itself a trading day
        if d.weekday() < 5 and d not in holidays:
            days.add(d)
    # ponytail: covers the three recurring half-days. When Christmas Eve is itself
    # the observed holiday the preceding day is left as a full session — the
    # conservative direction (we check a thin tape rather than skip a live one).
    return days


def market_close_time(d):
    return EARLY_CLOSE if d in early_close_days(d.year) else MARKET_CLOSE


def market_is_open(now=None):
    now = now or datetime.now(ET)
    d = now.date()
    if d.weekday() >= 5 or d in market_holidays(d.year):
        return False
    return MARKET_OPEN <= now.time() <= market_close_time(d)


def now_et():
    return datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")


def data_client():
    """The REST market-data client. Alpaca by default; Massive only on a paid tier
    (its free plan is end-of-day, which evaluates yesterday's prices as if live)."""
    return massive_client if config.DATA_PROVIDER == "massive" else alpaca_client


def _fetch_series(symbol, timeframe):
    mult, span, _disp, lookback = config.TIMEFRAMES[timeframe]
    return {"closes": data_client().get_bars(symbol, mult, span, lookback)}


def _provider(series_provider=None):
    """Series source for on-demand endpoints: caller override → demo → live Massive."""
    return series_provider or (demo_series if DEMO else _fetch_series)


def run_cycle(series_provider=None):
    """One tick, market-aware. THE entry point for both drivers.

    When the market is shut this is a healthy idle, not a failure — it refreshes
    the rule list, marks the heartbeat, and evaluates nothing. Skipping that
    distinction would fetch 24/7 on a paid key and email MONITOR DOWN every
    weekend and holiday.
    """
    if DEMO or market_is_open():
        run_once(series_provider)
    else:
        rows = [_row(r, conditions.describe(r["condition"]), None, False)
                for r in db.all_enabled_rules()]
        db.mark_success()
        _publish_snapshots(rows)
        logger.info("market closed, idling")
    _check_deadman()


def run_once(series_provider=None):
    provider = _provider(series_provider)  # honours DEMO, so no path reaches the live API in demo mode
    rules = db.all_enabled_rules()
    # ONE flat loop over every user's rules, not a per-user outer loop: `cache` is
    # what keeps 10 users watching NVDA 5min at a single paid upstream fetch, and
    # nesting by user is exactly the refactor that silently destroys that.
    # ponytail: unbounded fetches per cycle. A global ceiling here is the upgrade
    # path if signups ever outpace the market-data budget.
    cache = {}  # (symbol,timeframe) -> series, so each pair is fetched once per cycle
    rows, fires = [], {}  # fires: user_id -> [(rule, desc, value, price)]
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
                # history before delivery: if the process is killed mid-cycle the
                # alert is still recorded, rather than latched fired with no trace
                db.append_alert(rule, desc, value, price)
                fires.setdefault(rule["user_id"], []).append((rule, desc, value, price))
                logger.info("ALERT: %s [%s] value=%.2f price=%.2f", rule["name"], desc, value, price)
            rows.append(_row(rule, desc, value, fired))
        except Exception:
            logger.exception("error evaluating rule %s (%s)", rule.get("id"), rule.get("name"))
            rows.append(_row(rule, "error", None, False, error=True))
    for uid, user_fires in fires.items():
        # one batched request per user, not one per rule: a correlated selloff fires
        # many rules at once, and per-rule POSTs hit Resend's rate limit and the
        # function's wall clock together. try/except INSIDE the loop, so one user's
        # delivery failure cannot swallow everyone queued behind them.
        try:
            notifier.send_batch(user_fires, now_et(), db.user_recipients(uid))
        except Exception:
            logger.exception("alert delivery failed for user %s (%d rule(s))", uid, len(user_fires))
    global _last_error
    ok = (not rows) or any(not r.get("error") for r in rows)
    if ok:
        db.mark_success()
    elif rows:
        _last_error = "all rules failed to evaluate (market data unavailable?)"
    _publish_snapshots(rows)


def _row(rule, desc, value, fired, error=False):
    return {
        "id": rule["id"], "user_id": rule["user_id"], "name": rule["name"], "symbol": rule["symbol"],
        "timeframe": rule["timeframe"], "type": rule["condition"].get("type"),
        "description": desc, "value": None if value is None else round(value, 2),
        "fired": bool(fired), "enabled": True, "error": error,
    }


def _publish_snapshots(rows):
    """Store the cycle's view in the DB — a read-only filesystem has nowhere to put a file.

    One blob PER USER. A single global blob filtered on read would ship every
    user's rules to every dashboard's 3-second poll, and would sit in the same
    settings table as everyone else's watchlist.
    """
    base = {"updated": now_et(), "market_open": market_is_open(), "dry_run": config.DRY_RUN}
    by_user = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append(r)
    # ponytail: one upsert per user per cycle. Batch into a single executemany if
    # the cycle's wall clock ever gets tight.
    for uid, urows in by_user.items():
        db.set_setting(db.ukey(uid, "snapshot"), json.dumps({**base, "rules": urows}))


def snapshot(user_id):
    """The /api/snapshot payload for one user, assembled on read.

    `healthy` is computed here, not baked in by the writer, because the failure
    this switch exists to catch — the cron stops firing — means no writer runs at
    all. Deriving it on read is the only way that failure ever becomes visible.

    The health fields are global on purpose: there is one scheduler, so a dead
    cron means nobody's rules are being monitored and everybody should see it.
    """
    stored = db.get_setting(db.ukey(user_id, "snapshot"))
    base = json.loads(stored) if stored else {"updated": None, "rules": []}
    stale = db.stale_seconds()
    return {
        **base,
        "market_open": market_is_open(),
        "dry_run": config.DRY_RUN,
        "last_success_ms": db.last_success_ms(),
        "healthy": stale is not None and stale <= config.DEADMAN_SECONDS,
        "deadman_seconds": config.DEADMAN_SECONDS,
        "alerts": db.list_alerts(user_id, 50),
    }


def _check_deadman():
    """Fire the dead-man's switch once when the loop stops completing healthy cycles."""
    stale = db.stale_seconds()
    if stale is None or stale <= config.DEADMAN_SECONDS:
        if db.get_setting("deadman_fired"):
            db.set_setting("deadman_fired", "")
        return
    if db.get_setting("deadman_fired"):
        return
    logger.error("DEAD-MAN tripped: %.0fs since last healthy cycle", stale)
    try:
        notifier.send_deadman(_last_error or "no successful cycle", stale)
    except Exception:
        logger.exception("dead-man notify failed")
    db.set_setting("deadman_fired", "1")


def run_loop():
    logger.info("engine loop starting (refresh=%ss, deadman=%ss)", config.REFRESH_SECONDS, config.DEADMAN_SECONDS)
    global _last_error
    while True:
        try:
            run_cycle()
        except Exception as exc:
            _last_error = repr(exc)
            logger.exception("cycle error")
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
    return {"closes": list(closes)}


def run_demo():
    logger.info("engine DEMO mode — synthetic data, notifications dry-run")
    while True:
        try:
            run_once()  # DEMO is set, so _provider() resolves to demo_series
        except Exception:
            logger.exception("demo cycle error")
        time.sleep(2.5)


# --- on-demand endpoints (Watchlist / Backtest) ---

def watchlist(series_provider=None, ttl=20):
    """Live price + RSI for every watched symbol × timeframe (the Watchlist grid).

    The cache lives in the settings table, not a module global. Serverless gives
    every request a fresh process, so an in-memory cache is a guaranteed miss —
    which made each page-load 24 upstream fetches. A signed-in stranger holding
    F5 here burns market-data quota with zero rules, so the per-user rule cap
    does nothing about it. One global row: the grid is the same for everyone.
    """
    provider = _provider(series_provider)
    now = time.time()
    if series_provider is None:
        cached = db.get_setting("watchlist")
        if cached:
            blob = json.loads(cached)
            if now - blob["ts"] < ttl:
                return blob["data"]
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
        db.set_setting("watchlist", json.dumps({"ts": now, "data": out}))
    return out


def backtest(symbol, timeframe, condition, series_provider=None):
    """Replay a condition bar-by-bar over the lookback window; count fresh False→True fires."""
    provider = _provider(series_provider)
    closes = provider(symbol, timeframe)["closes"]
    fires, prev = [], False
    for i in range(max(conditions.min_bars(condition), 2), len(closes) + 1):
        try:
            fired, value = conditions.evaluate(condition, {"closes": closes[:i]})
        except Exception:
            continue  # not enough bars yet for this indicator
        if fired and not prev:
            fires.append({"index": i - 1, "value": round(value, 2), "price": round(closes[i - 1], 2)})
        prev = fired
    return {
        "symbol": symbol, "timeframe": timeframe, "description": conditions.describe(condition),
        "bars": len(closes), "fires": len(fires), "events": fires[-50:],
    }
