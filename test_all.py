"""Comprehensive assert-based tests for the v2 (multi-indicator, email) system.

Run: python test_all.py   (no framework, no network — uses a temp SQLite db + demo series)
"""
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import config

# Point persistence at a throwaway location BEFORE importing db/engine touch it.
_TMP = Path(tempfile.mkdtemp())
config.DB_FILE = _TMP / "test.db"
config.DATABASE_URL = ""  # force the SQLite path regardless of the developer's env
config.DRY_RUN = True  # forces notifier into dry-run (logs, never sends)

import ai
import alpaca_client
import conditions
import db
import engine
import notifier
from rsi import wilder_rsi, sma, ema, percent_change

ET = ZoneInfo("America/New_York")


def test_config():
    print("Testing config.py...")
    assert len(config.SYMBOLS) == 6 and "AAPL" in config.SYMBOLS
    assert len(config.TIMEFRAMES) == 4
    assert config.RSI_PERIOD == 14
    assert config.REFRESH_SECONDS == 30
    print("  ✓ config OK")


def test_indicators():
    print("Testing rsi.py (indicators)...")
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
              45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    r = wilder_rsi(closes, 14)
    assert 70.0 <= r <= 71.0, r
    assert wilder_rsi(list(range(1, 16))) == 100.0
    assert wilder_rsi(list(range(15, 0, -1))) == 0.0
    try:
        wilder_rsi([1, 2, 3]); raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert sma([1, 2, 3, 4], 4) == 2.5
    assert abs(ema([5, 5, 5, 5, 5], 5) - 5.0) < 1e-9
    assert percent_change([100, 110], 1) == 10.0
    assert percent_change([100, 90], 1) == -10.0
    print(f"  ✓ indicators OK (classic RSI={r:.2f})")


def test_conditions():
    print("Testing conditions.py...")
    assert len(conditions.REGISTRY) >= 5
    assert len(conditions.schema()) == 5

    up = {"closes": list(range(1, 40))}
    down = {"closes": list(range(40, 1, -1))}

    fired, val = conditions.evaluate({"type": "rsi", "threshold": 85, "direction": "above"}, up)
    assert fired and val == 100.0
    fired, val = conditions.evaluate({"type": "rsi", "threshold": 15, "direction": "below"}, down)
    assert fired and val == 0.0

    # rsi_band: fires when RSI leaves the 20–80 band (hits either bound)
    fired, val = conditions.evaluate({"type": "rsi_band", "low": 20, "high": 80}, up)
    assert fired and val == 100.0
    fired, _ = conditions.evaluate({"type": "rsi_band", "low": 0, "high": 100}, up)  # 0<=100<=100 → in band
    assert not fired

    fired, val = conditions.evaluate({"type": "price", "level": 30, "direction": "above"}, up)
    assert fired and val == 39

    fired, val = conditions.evaluate({"type": "percent_change", "pct": 3, "bars": 1, "direction": "up"},
                                     {"closes": [100, 105]})
    assert fired and val == 5.0

    fired, val = conditions.evaluate({"type": "ma_cross", "fast": 5, "slow": 20, "direction": "golden"}, up)
    assert fired and val > 0  # rising series → fast MA above slow MA

    # min_bars comes off the condition class, so every type answers — rsi_band
    # used to fall through to 1 and get replayed with too few closes
    assert conditions.min_bars({"type": "rsi", "period": 14}) == 15
    assert conditions.min_bars({"type": "rsi_band", "period": 14, "low": 20, "high": 80}) == 15
    assert conditions.min_bars({"type": "ma_cross", "fast": 9, "slow": 21, "direction": "golden"}) == 22
    assert conditions.min_bars({"type": "percent_change", "pct": 5, "bars": 3, "direction": "up"}) == 4
    assert conditions.min_bars({"type": "price", "level": 1, "direction": "above"}) == 1

    # validation rejects bad input
    for bad in ({"type": "rsi"}, {"type": "nope", "x": 1}):
        try:
            conditions.validate(bad); raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass
    print("  ✓ conditions OK (rsi/rsi_band/price/percent_change/ma_cross + min_bars + validation)")


def test_market_calendar():
    print("Testing engine NYSE calendar (vs published 2026/2027 dates)...")
    # NYSE's published full closures. 2026 Independence Day lands on a Saturday
    # and is observed Friday Jul 3; 2027 lands on a Sunday and moves to Mon Jul 5.
    expect_2026 = {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
    }
    expect_2027 = {
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
        date(2027, 11, 25), date(2027, 12, 24),
    }
    assert engine.market_holidays(2026) == expect_2026, sorted(engine.market_holidays(2026) ^ expect_2026)
    assert engine.market_holidays(2027) == expect_2027, sorted(engine.market_holidays(2027) ^ expect_2027)

    # 2022 had Jan 1 on a Saturday — the NYSE observes nothing at all in that case
    assert date(2021, 12, 31) not in engine.market_holidays(2022)
    assert date(2022, 1, 1) not in engine.market_holidays(2022)

    # half-days close at 13:00 ET, not 16:00
    assert date(2026, 11, 27) in engine.early_close_days(2026)   # Friday after Thanksgiving
    assert date(2026, 12, 24) in engine.early_close_days(2026)   # Christmas Eve
    assert engine.market_close_time(date(2026, 11, 27)) == engine.EARLY_CLOSE
    assert engine.market_close_time(date(2026, 11, 30)) == engine.MARKET_CLOSE

    # the gate itself
    assert engine.market_is_open(datetime(2026, 11, 26, 11, 0, tzinfo=ET)) is False   # Thanksgiving
    assert engine.market_is_open(datetime(2026, 11, 27, 11, 0, tzinfo=ET)) is True    # half-day, morning
    assert engine.market_is_open(datetime(2026, 11, 27, 14, 0, tzinfo=ET)) is False   # half-day, afternoon
    assert engine.market_is_open(datetime(2026, 11, 30, 14, 0, tzinfo=ET)) is True    # normal Monday
    assert engine.market_is_open(datetime(2026, 11, 28, 11, 0, tzinfo=ET)) is False   # Saturday
    assert engine.market_is_open(datetime(2026, 11, 30, 9, 0, tzinfo=ET)) is False    # pre-open
    print("  ✓ calendar OK (10 holidays/yr, observance rules, half-days)")


def test_db():
    print("Testing db.py...")
    db.init()
    rule = db.create_rule({"id": "r1", "name": "t", "symbol": "AAPL", "timeframe": "5min",
                           "condition": {"type": "rsi", "threshold": 15, "direction": "below"},
                           "enabled": True}, "userA")
    assert rule["id"] == "r1" and rule["condition"]["type"] == "rsi"
    assert rule["user_id"] == "userA"
    assert len(db.list_rules("userA")) == 1
    db.update_rule("r1", {**rule, "name": "t2", "enabled": False}, "userA")
    assert db.get_rule("r1", "userA")["name"] == "t2"
    assert db.list_rules("userA", enabled_only=True) == []

    # dedup state machine: fire once → hold → reset → re-fire.
    # Pinned to 0: ALERT_MIN_GAP_SECONDS defaults non-zero under live evaluation and
    # would (correctly) suppress the immediate re-fire this asserts. The gap has its
    # own test below; this one is about the edge machine itself.
    _gap = config.ALERT_MIN_GAP_SECONDS
    config.ALERT_MIN_GAP_SECONDS = 0
    assert db.record_transition("r1", True, 10) is True
    assert db.record_transition("r1", True, 9) is False
    assert db.record_transition("r1", False, 50) is False
    assert db.record_transition("r1", True, 8) is True
    config.ALERT_MIN_GAP_SECONDS = _gap

    # claim_once is the atomic guard against two cold starts both seeding
    assert db.claim_once("unit-test-claim") is True
    assert db.claim_once("unit-test-claim") is False

    db.append_alert(rule, "RSI below 15", 8.0, 150.0)
    assert len(db.list_alerts("userA")) == 1
    assert db.list_alerts("userA", limit=10_000) is not None  # limit is clamped, not trusted
    db.delete_rule("r1", "userA")
    assert db.list_rules("userA") == [] and db.get_rule("r1", "userA") is None
    print("  ✓ db OK (CRUD + fire-once/hold/reset/re-fire dedup + claim_once)")


def test_user_isolation():
    print("Testing per-user isolation (db layer)...")
    db.init()
    A, B = "isoA", "isoB"  # ids unique to this test: one SQLite file is shared by all of them
    mk = lambda rid: {"id": rid, "name": rid, "symbol": "AAPL", "timeframe": "5min",
                      "condition": {"type": "rsi", "threshold": 15, "direction": "below"}, "enabled": True}
    a1 = db.create_rule(mk("a1"), A)
    db.create_rule(mk("b1"), B)

    assert [r["id"] for r in db.list_rules(A)] == ["a1"]
    assert [r["id"] for r in db.list_rules(B)] == ["b1"]
    assert {r["id"] for r in db.all_enabled_rules()} >= {"a1", "b1"}  # the engine sees everyone

    # no cross-user read or write
    assert db.get_rule("a1", B) is None
    assert db.update_rule("a1", {**a1, "name": "pwned"}, B) is None
    assert db.get_rule("a1", A)["name"] != "pwned"

    # a foreign delete must not un-latch someone else's dedup state, or the next
    # cycle re-alerts them for a rule that never reset
    assert db.record_transition("a1", True, 10) is True
    db.delete_rule("a1", B)
    assert db.get_rule("a1", A) is not None, "foreign delete removed the rule"
    assert db.record_transition("a1", True, 10) is False, "foreign delete cleared rule_state"

    # alerts are scoped too, and history survives its rule being deleted
    db.append_alert(a1, "RSI below 15", 8.0, 150.0)
    assert len(db.list_alerts(A)) == 1 and db.list_alerts(B) == []
    db.delete_rule("a1", A)
    assert len(db.list_alerts(A)) == 1, "deleting a rule must not erase its history"

    # per-user recipients: never fall back to the operator's address
    config.DEFAULT_EMAIL_RECIPIENTS = ["ops@x.com"]
    db.set_user_recipients(A, ["a@x.com"])
    assert db.user_recipients(A) == ["a@x.com"]
    assert db.user_recipients("stranger") == [], "a stranger's alerts must not route to ops"
    print("  ✓ isolation OK (no cross-user read/write/delete, scoped alerts + recipients)")


def test_sqlite_migration():
    """A database written before per-user accounts must migrate, not crash.

    CREATE TABLE IF NOT EXISTS silently skips tables that already exist, so an
    existing dev file keeps the old shape until the ALTERs run — and anything that
    depends on the new columns (the user indexes) has to come after them, not
    before. A fresh-file test cannot catch that ordering.
    """
    print("Testing db.init() against a pre-user_id database...")
    import sqlite3
    legacy = _TMP / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript("""
        CREATE TABLE rules(id TEXT PRIMARY KEY, name TEXT, symbol TEXT, timeframe TEXT,
                           condition TEXT, enabled INTEGER DEFAULT 1, created_at TEXT);
        CREATE TABLE alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT, name TEXT,
                            symbol TEXT, timeframe TEXT, description TEXT,
                            value REAL, price REAL, ts TEXT);
        CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO rules VALUES('old1','legacy','SPY','5min',
            '{"type":"rsi","threshold":15,"direction":"below"}',1,'2026-01-01T00:00:00');
        INSERT INTO alerts(rule_id,name,symbol,timeframe,description,value,price,ts)
            VALUES('old1','legacy','SPY','5min','RSI below 15',9.0,500.0,'2026-01-01 10:00:00 ET');
    """)
    conn.commit()
    conn.close()

    prev = config.DB_FILE
    config.DB_FILE = legacy
    try:
        db.init()  # must not raise
        # legacy rows survive, owned by nobody, and are invisible to every account
        assert [r["id"] for r in db.list_rules("")] == ["old1"], "legacy rules lost in migration"
        assert db.list_rules("someone") == []
        assert len(db.list_alerts("")) == 1, "legacy history lost in migration"
        # the columns really landed, both the old ts_ms migration and the new one
        assert "ts_ms" in db.list_alerts("")[0]
        db.set_user_recipients("someone", ["x@y.com"])
        assert db.user_recipients("someone") == ["x@y.com"]
        db.init()  # idempotent: a second cold start must not re-ALTER and blow up
    finally:
        config.DB_FILE = prev
    print("  ✓ migration OK (legacy rows kept + unowned, indexes after ALTER, idempotent)")


def test_notifier_dry_run():
    print("Testing notifier.py (email-only, dry-run, batched)...")
    base = {"symbol": "AAPL", "timeframe": "5min", "name": "t"}
    ts = "2026-07-09 10:00:00 ET"
    to = ["a@x.com"]

    r = notifier.send(base, "RSI below 15", 12.3, 150.0, ts, to, dry_run=True)
    assert set(r) == {"email"} and r["email"]["sent"] is False
    assert r["email"]["subject"].endswith("RSI below 15")

    # one user's fires go out as ONE request, one result per fire
    fires = [(base, "RSI below 15", 12.3, 150.0), ({**base, "symbol": "NVDA"}, "RSI above 85", 91.0, 180.0)]
    rs = notifier.send_batch(fires, ts, to, dry_run=True)
    assert len(rs) == 2 and all(x["sent"] is False for x in rs)
    assert "NVDA" in rs[1]["subject"] and rs[0]["to"] == to
    assert notifier.send_batch([], ts, to, dry_run=True) == []

    # a user with no recipients logs instead of sending — it must NOT reach for
    # DEFAULT_EMAIL_RECIPIENTS, which would post their alerts to the operator
    config.DEFAULT_EMAIL_RECIPIENTS = ["ops@x.com"]
    assert all(x["to"] == [] for x in notifier.send_batch(fires, ts, [], dry_run=True))

    # the dead-man email is ops mail: it reads config, never the database, because
    # an unreachable db is a leading cause of the dead cycle it exists to report
    real = db.get_setting
    db.get_setting = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
    try:
        assert notifier.send_deadman("x", 999, dry_run=True)["to"] == ["ops@x.com"]
    finally:
        db.get_setting = real
    print("  ✓ notifier OK (per-user batch, no operator fallback, deadman needs no db)")


def test_engine_cycle():
    print("Testing engine.run_once (demo series, dedup, per-user delivery)...")
    db.init()
    A, B = "engA", "engB"  # ids unique to this test: one SQLite file is shared by all of them
    # every OTHER user's rules must be off the books, or their fires land in the
    # per-user delivery assertions below
    for r in db.all_enabled_rules():
        db.delete_rule(r["id"], r["user_id"])
    # …and for the same reason, drain the delivery outbox: alerts other tests wrote
    # directly are legitimately undelivered, so a cycle now retries them and their
    # batches would land in the per-user assertions below.
    db.mark_alerts_sent([a["id"] for a in db.unsent_alerts(max_attempts=1 << 30,
                                                           max_age_minutes=1 << 30)])
    # rules that always fire (price below a huge level) → tests fire-once + snapshot.
    # Both users watch the SAME symbol×timeframe, which is what makes the cache
    # assertion below meaningful.
    always = {"type": "price", "level": 1_000_000_000, "direction": "below"}
    db.create_rule({"id": "always", "name": "always", "symbol": "AAPL", "timeframe": "5min",
                    "condition": always, "enabled": True}, A)
    db.create_rule({"id": "alwaysB", "name": "alwaysB", "symbol": "AAPL", "timeframe": "5min",
                    "condition": always, "enabled": True}, B)
    db.set_user_recipients(A, ["a@x.com"])
    db.set_user_recipients(B, ["b@x.com"])

    # one upstream fetch per symbol×timeframe per cycle, no matter how many users
    # share it. Grouping fires by user invites a per-user outer loop, which would
    # silently multiply the paid market-data bill by the user count.
    calls, sent = [], []
    def counting(sym, tf):
        calls.append((sym, tf))
        return engine.demo_series(sym, tf)
    real_send = notifier.send_batch
    notifier.send_batch = lambda fires, ts, recipients, **k: sent.append(
        (tuple(recipients), {f[0]["user_id"] for f in fires}))
    try:
        engine.run_once(series_provider=counting)
    finally:
        notifier.send_batch = real_send
    assert calls.count(("AAPL", "5min")) == 1, f"one fetch per pair per cycle, got {calls}"
    assert len(sent) == 2, f"one request per user with fires, not one global batch: {sent}"
    assert all(len(uids) == 1 for _to, uids in sent), f"a batch carried another user's rule: {sent}"
    assert {to for to, _ in sent} == {("a@x.com",), ("b@x.com",)}, sent

    # snapshots are per-user: each sees only their own rules and alerts
    snap = engine.snapshot(A)
    assert [r["id"] for r in snap["rules"]] == ["always"]
    assert snap["rules"][0]["fired"] is True
    assert all(a["rule_id"] == "always" for a in snap["alerts"])
    assert snap["healthy"] is True and snap["last_success_ms"], "snapshot missing dead-man heartbeat"
    # a user with no rules still gets working global health fields
    fresh = engine.snapshot("nobody")
    assert fresh["rules"] == [] and fresh["healthy"] is True

    n1 = len(db.list_alerts(A))
    engine.run_once(series_provider=engine.demo_series)  # still fired, not fresh
    assert len(db.list_alerts(A)) == n1, "dedup failed: alert re-fired while held"
    # alerts carry epoch-ms for the "triggered X ago" counter
    assert db.list_alerts(A)[0]["ts_ms"] and db.list_alerts(A)[0]["ts_ms"] > 0

    # `healthy` is derived on read, so a stalled cron surfaces even though nothing
    # ran to write it — the failure the dead-man switch exists to catch
    db.set_setting("last_success_ms", 0)
    assert engine.snapshot(A)["healthy"] is False, "stale heartbeat must read as unhealthy"
    db.mark_success()
    print(f"  ✓ engine OK ({n1} alert, shared fetch cache, per-user batches + snapshots)")


def test_backtest_and_watchlist():
    print("Testing engine.backtest + engine.watchlist (injected series)...")
    rising = {"closes": list(range(1, 60))}
    res = engine.backtest("AAPL", "5min", {"type": "rsi", "threshold": 90, "direction": "above"},
                          series_provider=lambda s, t: rising)
    assert res["bars"] == 59 and res["fires"] == 1, res  # RSI pegs 100 → fires once on the edge, no re-fire
    wl = engine.watchlist(series_provider=lambda s, t: {"closes": list(range(1, 40))})
    assert len(wl) == len(config.SYMBOLS)
    assert wl[0]["price"] == 39 and wl[0]["rows"][0]["rsi"] == 100.0
    print(f"  ✓ backtest/watchlist OK (1 fire over 59 bars, {len(wl)} symbols)")


def test_stream_aggregation():
    """Folding streamed 1-minute bars into 5/15/60-minute series.

    This is the only genuinely new logic in the websocket path, and it is the kind
    that fails silently: an off-by-one in bucket completion shifts every alert by
    one bar, and a timezone slip shifts hourly buckets by an hour for half the year.
    """
    print("Testing stream.py (bar bucketing, no network)...")
    import stream

    # timestamps are UTC and must be read as UTC. mktime() would read them as
    # local time, and `- time.timezone` ignores DST.
    assert stream.epoch_minute("2026-08-17T13:35:00Z") % 60 == 35
    assert stream.epoch_minute("2026-01-17T13:35:00Z") % 60 == 35   # DST-free month agrees
    assert stream.epoch_minute("2026-08-17T14:00:00Z") % 60 == 0
    base = stream.epoch_minute("2026-08-17T13:00:00Z")
    assert stream.epoch_minute("2026-08-17T14:00:00Z") - base == 60

    stream._SERIES.clear(); stream._PENDING.clear()
    for tf in ("1min", "5min", "15min", "1hour"):
        stream._SERIES[("TEST", tf)] = []

    # one clean UTC hour, 13:00-13:59, close = minute number
    for m in range(60):
        stream.apply_bar("TEST", base + m, float(m))

    # 1min takes every bar; the others only on bucket close, and the value stored
    # is the LAST minute of the bucket, not the first
    assert stream._SERIES[("TEST", "1min")] == [float(m) for m in range(60)]
    assert stream._SERIES[("TEST", "5min")] == [4.0, 9.0, 14.0, 19.0, 24.0, 29.0,
                                                34.0, 39.0, 44.0, 49.0, 54.0, 59.0]
    assert stream._SERIES[("TEST", "15min")] == [14.0, 29.0, 44.0, 59.0]
    assert stream._SERIES[("TEST", "1hour")] == [59.0]

    # a bucket only closes on its LAST minute — no lag, and no early fire
    stream._SERIES[("TEST", "5min")] = []
    for m in range(60, 64):                      # 14:00-14:03, bucket incomplete
        stream.apply_bar("TEST", base + m, 1.0)
    assert stream._SERIES[("TEST", "5min")] == [], "bucket closed early"
    assert stream.apply_bar("TEST", base + 64, 7.0) is True, "bucket did not close on its last minute"
    assert stream._SERIES[("TEST", "5min")] == [7.0]

    # pairs nobody has a rule for are ignored rather than silently accumulating
    assert stream.apply_bar("NOSUCH", base, 1.0) is False
    assert ("NOSUCH", "5min") not in stream._SERIES

    # series stay bounded — this runs for months without a restart
    stream._SERIES[("TEST", "1min")] = [0.0] * (alpaca_client.MAX_BARS + 50)
    stream.apply_bar("TEST", base + 200, 1.0)
    assert len(stream._SERIES[("TEST", "1min")]) == alpaca_client.MAX_BARS

    # the symbol cap must drop deterministically, not by set-iteration luck
    saved = config.MAX_STREAM_SYMBOLS
    config.MAX_STREAM_SYMBOLS = 2
    try:
        got = stream.wanted_symbols({("NVDA", "5min"), ("AAPL", "5min"), ("SPY", "1min")})
        assert got == ["AAPL", "NVDA"], got
    finally:
        config.MAX_STREAM_SYMBOLS = saved

    stream._SERIES.clear(); stream._PENDING.clear()
    print("  ✓ stream OK (UTC buckets, close-on-last-minute, bounded, deterministic cap)")


def test_api_auth():
    print("Testing main.py routes (Google session gate, ownership, cap, cron secret)...")
    from fastapi.testclient import TestClient
    import main

    config.DB_FILE = _TMP / "api.db"          # a clean db: accounts seed on first read
    config.SUPABASE_URL = "https://example.supabase.co"
    config.SESSION_SECRET = "t3st-secret"
    config.AUTH_TOKEN = "s3cret"
    config.CRON_SECRET = "cr0n"
    config.MAX_RULES_PER_USER = 4
    good = {"name": "t", "symbol": "AAPL", "timeframe": "5min",
            "condition": {"type": "rsi", "threshold": 15, "direction": "below"}}

    with TestClient(main.app) as c:
        # unauthenticated: the API refuses, "/" serves the public landing page
        assert c.get("/api/rules").status_code == 401
        assert c.put("/api/settings", json={"email_recipients": ["x@y.com"]}).status_code == 401
        r = c.get("/")
        assert r.status_code == 200 and "Set the rule" in r.text, "landing must serve unauthenticated"
        assert "page-dashboard" not in r.text, "shell must not render unauthenticated"

        # the public pages are reachable with no session at all — a sign-up page
        # behind the gate is a sign-up page nobody can use
        assert "Continue with Google" in c.get("/signin").text
        assert c.get("/signup").status_code == 200
        assert c.get("/site.css").status_code == 200 and c.get("/site.js").status_code == 200
        # an unknown address is a 404 PAGE, not a bare 401, and not the shell
        miss = c.get("/no-such-page")
        assert miss.status_code == 404 and "Nothing on this frequency" in miss.text
        assert c.get("/api/no-such-thing").status_code == 401   # but the API still refuses first
        assert c.get("/docs", follow_redirects=False).status_code == 303

        # a signed session is accepted; a tampered or expired one is not
        c.cookies.set(config.SESSION_COOKIE, main.make_session("u1", "u1@x.com"))
        assert c.get("/api/rules").status_code == 200
        assert "page-dashboard" in c.get("/").text, "signed in, the shell must render"
        assert c.get("/api/meta").json()["user"] == "u1@x.com"
        # signed in, an unknown page still gets the 404 page and the API still gets JSON
        assert c.get("/no-such-page").status_code == 404
        assert c.get("/api/no-such-thing").json()["detail"] == "Not Found"

        tampered = main.make_session("u1", "u1@x.com")
        tampered = tampered[:-2] + ("aa" if tampered[-2:] != "aa" else "bb")
        c.cookies.set(config.SESSION_COOKIE, tampered)
        assert c.get("/api/rules").status_code == 401, "forged signature must not authenticate"
        c.cookies.set(config.SESSION_COOKIE, main.make_session("u1", "u1@x.com", days=-1))
        assert c.get("/api/rules").status_code == 401, "expired session must not authenticate"

        # bearer works for API clients that cannot hold a cookie
        c.cookies.clear()
        assert c.get("/api/rules", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/api/rules", headers={"Authorization": "Bearer nope"}).status_code == 401

        c.cookies.set(config.SESSION_COOKIE, main.make_session("u1", "u1@x.com"))
        # unknown keys are rejected, so a stale client cannot silently write junk
        assert c.post("/api/rules", json={**good, "channels": ["sms"]}).status_code == 422
        # a symbol reaches an upstream URL path carrying our API key
        assert c.post("/api/rules", json={**good, "symbol": "../../etc"}).status_code == 422
        assert c.post("/api/rules", json={**good, "timeframe": "3min"}).status_code == 422
        assert c.get("/api/alerts?limit=99999").status_code == 422

        # open signup on a paid data key: the per-account rule cap has to bite
        while len(c.get("/api/rules").json()) < config.MAX_RULES_PER_USER:
            assert c.post("/api/rules", json=good).status_code == 200
        assert c.post("/api/rules", json=good).status_code == 403, "rule cap not enforced"

        # ownership: another account cannot see, edit or delete u1's rule — and gets
        # 404 rather than 403, so the id is never confirmed to exist
        rid = c.get("/api/rules").json()[0]["id"]
        c.cookies.set(config.SESSION_COOKIE, main.make_session("u2", "u2@x.com"))
        assert rid not in [r["id"] for r in c.get("/api/rules").json()]
        assert c.put(f"/api/rules/{rid}", json=good).status_code == 404
        assert c.delete(f"/api/rules/{rid}").status_code == 404
        # recipients are per-account too
        c.put("/api/settings", json={"email_recipients": ["u2@x.com"]})
        c.cookies.set(config.SESSION_COOKIE, main.make_session("u1", "u1@x.com"))
        assert c.get("/api/settings").json()["email_recipients"] != ["u2@x.com"]

        # the cron endpoint takes its OWN secret — a user's session must not work
        c.cookies.clear()
        assert c.get("/api/cron/evaluate").status_code == 401
        assert c.get("/api/cron/evaluate", headers={"Authorization": "Bearer s3cret"}).status_code == 401
        engine.DEMO = True  # keep the cycle off the network
        try:
            assert c.get("/api/cron/evaluate", headers={"Authorization": "Bearer cr0n"}).status_code == 200
        finally:
            engine.DEMO = False

    config.SUPABASE_URL = ""
    config.SESSION_SECRET = ""
    config.AUTH_TOKEN = ""
    config.CRON_SECRET = ""
    config.MAX_RULES_PER_USER = 25
    config.DB_FILE = _TMP / "test.db"
    print("  ✓ api OK (session sign/tamper/expiry, bearer, ownership 404, rule cap, cron secret)")


def test_ai():
    print("Testing ai.py (local NL rule builder + insights, stubbed inference)...")

    class FakeLLM:
        def __init__(self, content):
            self._content = content

        def create_chat_completion(self, **kwargs):  # ignores schema/messages, returns canned text
            return {"choices": [{"message": {"content": self._content}}]}

    # parse_rule maps a canned JSON completion to a rule dict conditions.validate() accepts
    ai._load_failed = False
    ai._llm = FakeLLM('{"name":"NVDA 5m RSI oversold","symbol":"nvda","timeframe":"5min",'
                      '"condition":{"type":"rsi","period":14,"threshold":15,"direction":"below"}}')
    draft = ai.parse_rule("email me when NVDA 5 minute RSI drops below 15")
    assert draft["symbol"] == "NVDA" and draft["timeframe"] == "5min", draft
    conditions.validate(draft["condition"])  # must not raise

    # summarize_insights returns the model text
    ai._llm = FakeLLM("SPY has been the most active, firing 6 times near the open.")
    assert "SPY" in ai.summarize_insights({"total": 6, "by_symbol": {"SPY": 6}, "most_active": "SPY"})

    # fail-open: no model + not loadable -> AIUnavailable (never crashes the caller)
    ai._llm = None
    ai._load_failed = True
    for fn in (lambda: ai.parse_rule("x"), lambda: ai.summarize_insights({})):
        try:
            fn(); raise AssertionError("expected AIUnavailable")
        except ai.AIUnavailable:
            pass
    ai._llm = None
    ai._load_failed = False
    print("  ✓ ai OK (parse_rule validates, summary returns text, fails open)")


def test_boundary_alignment():
    print("Testing engine.next_boundary (bar-close alignment)...")
    et = engine.ET
    at = lambda h, m, s=0: datetime(2026, 8, 17, h, m, s, tzinfo=et)

    # the next close for each timeframe, from a moment mid-bar
    assert engine.next_boundary({"1min"}, at(10, 7, 30)) == at(10, 8)
    assert engine.next_boundary({"5min"}, at(10, 7, 30)) == at(10, 10)
    assert engine.next_boundary({"15min"}, at(10, 7, 30)) == at(10, 15)
    assert engine.next_boundary({"1hour"}, at(10, 7, 30)) == at(11, 0)

    # mixed timeframes align to the SHORTEST — the 5min bar closes first, and a
    # cycle evaluates every rule at once anyway
    assert engine.next_boundary({"15min", "5min"}, at(10, 7, 30)) == at(10, 10)

    # exactly ON a boundary means that bar just closed; the next one is a full step
    assert engine.next_boundary({"5min"}, at(10, 10, 0)) == at(10, 15)

    # hour and day rollover
    assert engine.next_boundary({"15min"}, at(10, 52)) == at(11, 0)
    assert engine.next_boundary({"5min"}, at(23, 58)) == \
        datetime(2026, 8, 18, 0, 0, tzinfo=et)

    # unknown/empty timeframes must not crash the cron path
    assert engine.next_boundary(set()) is None
    assert engine.next_boundary({"7min"}) is None
    assert engine.previous_boundary(set()) is None
    assert engine.bar_step_minutes({"15min", "5min"}) == 5

    # previous_boundary drives the scheduler: the bar that JUST closed is the one
    # worth waiting for. Aligning to the NEXT one instead would add a whole interval
    # of latency to the invocation for nothing.
    assert engine.previous_boundary({"15min"}, at(10, 0, 4)) == at(10, 0)
    assert engine.previous_boundary({"15min"}, at(10, 7, 30)) == at(10, 0)
    assert engine.previous_boundary({"15min"}, at(10, 14, 59)) == at(10, 0)
    assert engine.previous_boundary({"15min"}, at(10, 15, 0)) == at(10, 15), "on a close, that IS the last bar"
    assert engine.previous_boundary({"1min"}, at(10, 0, 4)) == at(10, 0)

    # the resulting decision: sleep only just after a close, never for a stale one
    def wait_for(tfs, t):
        b = engine.previous_boundary(tfs, t)
        return (b + timedelta(seconds=18) - t).total_seconds()
    assert wait_for({"15min"}, at(10, 0, 4)) == 14, "a ping just after the close waits for the bar"
    assert wait_for({"15min"}, at(10, 0, 25)) < 0, "already published -> evaluate immediately"
    assert wait_for({"15min"}, at(10, 7, 30)) < 0, "mid-bar -> nothing new to wait for"
    print("  ✓ boundary OK (next/previous, shortest wins, on-boundary, rollover)")


def test_close_grace():
    print("Testing the close grace window (last bar of the session)...")
    et = engine.ET
    # a Monday, regular 16:00 close
    just_after = datetime(2026, 8, 17, 16, 0, 30, tzinfo=et)
    assert engine.market_is_open(just_after) is False, "strict gate must still say closed"
    assert engine.market_is_open(just_after, grace_seconds=120) is True, \
        "the 16:00 bar publishes after the close and must still be evaluable"
    # the grace is bounded, not an open door
    assert engine.market_is_open(datetime(2026, 8, 17, 16, 5, tzinfo=et),
                                 grace_seconds=120) is False
    # and it never opens the market early or on a weekend
    assert engine.market_is_open(datetime(2026, 8, 17, 9, 29, tzinfo=et),
                                 grace_seconds=120) is False
    assert engine.market_is_open(datetime(2026, 8, 15, 12, 0, tzinfo=et),
                                 grace_seconds=120) is False
    print("  ✓ grace OK (final bar evaluable, pill stays strict, bounded)")


def test_cooldown_rearm():
    print("Testing the cooldown re-arm (a held condition must not go silent forever)...")
    db.init()
    rid = "cooldown-rule"
    db.set_setting("_", "_")

    original = config.RULE_COOLDOWN_MINUTES
    gap_original = config.ALERT_MIN_GAP_SECONDS
    try:
        config.RULE_COOLDOWN_MINUTES = 30
        # This test is about the cooldown (a rule that STAYS true). The min-gap is a
        # separate guard for live evaluation and has its own test; leaving it on here
        # would suppress the fresh edges this asserts.
        config.ALERT_MIN_GAP_SECONDS = 0
        assert db.record_transition(rid, True, 10.0) is True, "first True must fire"
        assert db.record_transition(rid, True, 10.0) is False, "still held, inside cooldown"

        # wind last_fired_at back past the window -> the rule re-arms
        stale = (datetime.now(db.ET) - timedelta(minutes=31)).isoformat()
        with db._db(write=True) as c:
            db._exec(c, "UPDATE rule_state SET last_fired_at=? WHERE rule_id=?", (stale, rid))
        assert db.record_transition(rid, True, 10.0) is True, "must re-fire after the cooldown"
        assert db.record_transition(rid, True, 10.0) is False, "and latch again immediately"

        # a second cycle racing the same re-arm must not also win it
        with db._db(write=True) as c:
            db._exec(c, "UPDATE rule_state SET last_fired_at=? WHERE rule_id=?", (stale, rid))
        wins = [db.record_transition(rid, True, 10.0) for _ in range(3)]
        assert wins.count(True) == 1, f"exactly one caller may claim a re-arm: {wins}"

        # condition clearing still re-arms immediately, as before
        assert db.record_transition(rid, False, 50.0) is False
        assert db.record_transition(rid, True, 10.0) is True, "a fresh edge always fires"

        # 0 disables it: pure edge-triggered, today's behaviour
        config.RULE_COOLDOWN_MINUTES = 0
        with db._db(write=True) as c:
            db._exec(c, "UPDATE rule_state SET last_fired_at=? WHERE rule_id=?", (stale, rid))
        assert db.record_transition(rid, True, 10.0) is False, "cooldown=0 must never re-arm"
    finally:
        config.RULE_COOLDOWN_MINUTES = original
        config.ALERT_MIN_GAP_SECONDS = gap_original
    print("  ✓ cooldown OK (re-arms once, race-safe, clears on edge, 0 disables)")


def test_live_price_evaluation():
    print("Testing live (intra-bar) evaluation + flap guard...")
    # the live price is appended as a PROVISIONAL final bar, so a move is evaluated
    # now rather than at the next bar close
    original = dict(engine._live_prices)
    try:
        engine._live_prices = {"AAPL": 999.0}
        called = {}

        class FakeClient:
            SYMBOL_RE = alpaca_client.SYMBOL_RE
            @staticmethod
            def get_bars(sym, mult, span, lookback):
                called["hit"] = True
                return [100.0, 101.0, 102.0]

        real = engine.data_client
        engine.data_client = lambda: FakeClient
        try:
            series = engine._fetch_series("AAPL", "5min")
        finally:
            engine.data_client = real
        assert series["closes"] == [100.0, 101.0, 102.0, 999.0], series
        assert called.get("hit"), "the closed-bar history is still fetched"

        # a symbol with no live quote falls back to closed bars exactly as before
        engine._live_prices = {}
        engine.data_client = lambda: FakeClient
        try:
            assert engine._fetch_series("AAPL", "5min")["closes"] == [100.0, 101.0, 102.0]
        finally:
            engine.data_client = real
    finally:
        engine._live_prices = original

    # latest_prices must never raise — a dead quote endpoint degrades to closed bars
    real_key = config.ALPACA_API_KEY
    config.ALPACA_API_KEY = ""
    try:
        assert alpaca_client.latest_prices(["SPY"]) == {}, "no creds -> empty, not an exception"
    finally:
        config.ALPACA_API_KEY = real_key

    # --- the flap guard -----------------------------------------------------
    db.init()
    rid = "flap-rule"
    gap_original = config.ALERT_MIN_GAP_SECONDS
    cd_original = config.RULE_COOLDOWN_MINUTES
    try:
        config.ALERT_MIN_GAP_SECONDS = 300
        config.RULE_COOLDOWN_MINUTES = 30
        assert db.record_transition(rid, True, 10.0) is True, "first crossing alerts"
        # cross back out and in again, as an oscillating value does inside one bar
        for _ in range(5):
            assert db.record_transition(rid, False, 50.0) is False
            assert db.record_transition(rid, True, 10.0) is False, \
                "a re-crossing inside the gap must NOT alert again"
        # state still reflects reality even while alerts are suppressed
        with db._db() as c:
            row = db._exec(c, "SELECT fired FROM rule_state WHERE rule_id=?", (rid,)).fetchone()
        assert row["fired"] == 1, "the rule is latched even though the alert was suppressed"

        # once the gap has elapsed, a genuine new crossing alerts again
        stale = (datetime.now(db.ET) - timedelta(seconds=400)).isoformat()
        db.record_transition(rid, False, 50.0)
        with db._db(write=True) as c:
            db._exec(c, "UPDATE rule_state SET last_fired_at=? WHERE rule_id=?", (stale, rid))
        assert db.record_transition(rid, True, 10.0) is True, "past the gap, a crossing alerts"

        # gap 0 restores pure edge-triggered firing (closed-bar deployments)
        config.ALERT_MIN_GAP_SECONDS = 0
        assert db.record_transition(rid, False, 50.0) is False
        assert db.record_transition(rid, True, 10.0) is True, "gap=0 -> every edge fires"
    finally:
        config.ALERT_MIN_GAP_SECONDS = gap_original
        config.RULE_COOLDOWN_MINUTES = cd_original
    print("  ✓ live OK (provisional bar appended, degrades safely, flapping suppressed)")


def test_delivery_outbox():
    print("Testing the delivery outbox (a killed cycle must not lose the email)...")
    db.init()
    uid = "outbox-user"
    rule = {"id": "ob-rule", "user_id": uid, "name": "ob", "symbol": "SPY", "timeframe": "5min"}

    aid = db.append_alert(rule, "RSI(14) below 15", 12.0, 400.0)
    assert aid, "append_alert must return the new row id"
    pending = [a["id"] for a in db.unsent_alerts()]
    assert aid in pending, "a new alert starts UNSENT so a killed cycle can retry it"

    db.mark_alerts_sent([aid])
    assert aid not in [a["id"] for a in db.unsent_alerts()], "delivered alerts leave the outbox"

    # a failing send leaves it queued and counts the attempt
    aid2 = db.append_alert(rule, "RSI(14) below 15", 11.0, 399.0)
    db.bump_attempts([aid2])
    row = [a for a in db.unsent_alerts() if a["id"] == aid2]
    assert row and row[0]["attempts"] == 1, "a failed send must record an attempt"

    # attempts are bounded — a poison alert stops rather than wedging every cycle
    for _ in range(config.DELIVERY_MAX_ATTEMPTS):
        db.bump_attempts([aid2])
    assert aid2 not in [a["id"] for a in db.unsent_alerts()], "retries must be bounded"

    # …and so is age
    aid3 = db.append_alert(rule, "old one", 10.0, 398.0)
    with db._db(write=True) as c:
        db._exec(c, "UPDATE alerts SET ts_ms=? WHERE id=?",
                 (int((time.time() - 999 * 60) * 1000), aid3))
    assert aid3 not in [a["id"] for a in db.unsent_alerts()], "stale alerts age out of the outbox"

    # the engine retries a stranded alert on the next cycle, batched per user
    db.mark_alerts_sent([a["id"] for a in db.unsent_alerts(max_attempts=1 << 30,
                                                           max_age_minutes=1 << 30)])
    stranded = db.append_alert(rule, "stranded", 9.0, 397.0)
    db.set_user_recipients(uid, ["ob@x.com"])
    sent = []
    real = notifier.send_batch
    notifier.send_batch = lambda fires, ts, recipients, **k: sent.append(len(fires))
    try:
        engine._deliver(engine._retry_unsent())
    finally:
        notifier.send_batch = real
    assert sent and sum(sent) >= 1, f"a stranded alert must be re-sent: {sent}"
    assert stranded not in [a["id"] for a in db.unsent_alerts()], \
        "a successful retry must clear the alert from the outbox"
    print("  ✓ outbox OK (unsent by default, bounded by attempts+age, retried and cleared)")


def main():
    print("=" * 60)
    print("Market Alert System — comprehensive tests (v2)")
    print("=" * 60)
    tests = [test_config, test_indicators, test_conditions, test_market_calendar, test_db,
             test_user_isolation, test_sqlite_migration, test_notifier_dry_run,
             test_boundary_alignment, test_close_grace, test_cooldown_rearm,
             test_live_price_evaluation, test_delivery_outbox, test_engine_cycle,
             test_backtest_and_watchlist, test_stream_aggregation, test_api_auth, test_ai]
    passed = failed = 0
    for t in tests:
        try:
            t(); passed += 1
        except Exception as e:
            import traceback
            print(f"  ✗ FAILED: {e}")
            traceback.print_exc()
            failed += 1
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
