"""Comprehensive assert-based tests for the v2 (multi-indicator, email) system.

Run: python test_all.py   (no framework, no network — uses a temp SQLite db + demo series)
"""
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config

# Point persistence at a throwaway location BEFORE importing db/engine touch it.
_TMP = Path(tempfile.mkdtemp())
config.DB_FILE = _TMP / "test.db"
config.DATABASE_URL = ""  # force the SQLite path regardless of the developer's env
config.DRY_RUN = True  # forces notifier into dry-run (logs, never sends)

import ai
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
                           "enabled": True})
    assert rule["id"] == "r1" and rule["condition"]["type"] == "rsi"
    assert len(db.list_rules()) == 1
    db.update_rule("r1", {**rule, "name": "t2", "enabled": False})
    assert db.get_rule("r1")["name"] == "t2"
    assert db.list_rules(enabled_only=True) == []

    # dedup state machine: fire once → hold → reset → re-fire
    assert db.record_transition("r1", True, 10) is True
    assert db.record_transition("r1", True, 9) is False
    assert db.record_transition("r1", False, 50) is False
    assert db.record_transition("r1", True, 8) is True

    # claim_once is the atomic guard against two cold starts both seeding
    assert db.claim_once("unit-test-claim") is True
    assert db.claim_once("unit-test-claim") is False

    db.append_alert(rule, "RSI below 15", 8.0, 150.0)
    assert len(db.list_alerts()) == 1
    assert db.list_alerts(limit=10_000) is not None  # limit is clamped, not trusted
    db.delete_rule("r1")
    assert db.list_rules() == [] and db.get_rule("r1") is None
    print("  ✓ db OK (CRUD + fire-once/hold/reset/re-fire dedup + claim_once)")


def test_notifier_dry_run():
    print("Testing notifier.py (email-only, dry-run, batched)...")
    base = {"symbol": "AAPL", "timeframe": "5min", "name": "t"}
    ts = "2026-07-09 10:00:00 ET"

    r = notifier.send(base, "RSI below 15", 12.3, 150.0, ts, dry_run=True)
    assert set(r) == {"email"} and r["email"]["sent"] is False
    assert r["email"]["subject"].endswith("RSI below 15")

    # a cycle's fires go out as ONE request, one result per fire
    fires = [(base, "RSI below 15", 12.3, 150.0), ({**base, "symbol": "NVDA"}, "RSI above 85", 91.0, 180.0)]
    rs = notifier.send_batch(fires, ts, dry_run=True)
    assert len(rs) == 2 and all(x["sent"] is False for x in rs)
    assert "NVDA" in rs[1]["subject"]
    assert notifier.send_batch([], ts, dry_run=True) == []
    print("  ✓ notifier OK (email-only, dry-run, batch)")


def test_engine_cycle():
    print("Testing engine.run_once (demo series, dedup)...")
    db.init()
    for r in db.list_rules():
        db.delete_rule(r["id"])
    # a rule that always fires (price below a huge level) → tests fire-once + snapshot
    db.create_rule({"id": "always", "name": "always", "symbol": "AAPL", "timeframe": "5min",
                    "condition": {"type": "price", "level": 1_000_000_000, "direction": "below"},
                    "enabled": True})
    engine.run_once(series_provider=engine.demo_series)
    snap = engine.snapshot()
    assert snap["rules"] and snap["rules"][0]["fired"] is True
    assert snap["healthy"] is True and snap["last_success_ms"], "snapshot missing dead-man heartbeat"
    n1 = len(db.list_alerts())
    engine.run_once(series_provider=engine.demo_series)  # still fired, not fresh
    assert len(db.list_alerts()) == n1, "dedup failed: alert re-fired while held"
    # alerts carry epoch-ms for the "triggered X ago" counter
    assert db.list_alerts()[0]["ts_ms"] and db.list_alerts()[0]["ts_ms"] > 0

    # `healthy` is derived on read, so a stalled cron surfaces even though nothing
    # ran to write it — the failure the dead-man switch exists to catch
    db.set_setting("last_success_ms", 0)
    assert engine.snapshot()["healthy"] is False, "stale heartbeat must read as unhealthy"
    db.mark_success()
    print(f"  ✓ engine OK (snapshot from db, {n1} alert, no re-fire while held, staleness on read)")


def test_settings_recipients():
    print("Testing db settings + notifier recipient resolution...")
    db.init()
    config.DEFAULT_EMAIL_RECIPIENTS = ["fallback@x.com"]
    db.set_setting("email_recipients", "")
    assert notifier._recipients() == ["fallback@x.com"]          # no setting → .env default
    db.set_setting("email_recipients", "a@x.com, b@y.com")
    assert notifier._recipients() == ["a@x.com", "b@y.com"]      # saved setting wins

    # an unreachable database must not silence the MONITOR DOWN email
    real = db.get_setting
    db.get_setting = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
    try:
        assert notifier._recipients() == ["fallback@x.com"], "deadman must fall back when the db throws"
    finally:
        db.get_setting = real
    print("  ✓ settings OK (saved > .env, and env fallback when the db is unreachable)")


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


def test_api_auth():
    print("Testing main.py routes (auth gate, validation, cron secret)...")
    from fastapi.testclient import TestClient
    import main

    config.DB_FILE = _TMP / "api.db"          # a clean db: the lifespan seeds starter rules
    config.AUTH_TOKEN = "s3cret"
    config.CRON_SECRET = "cr0n"

    with TestClient(main.app) as c:
        # unauthenticated: the API refuses, the dashboard shell serves an unlock page
        assert c.get("/api/rules").status_code == 401
        assert c.put("/api/settings", json={"email_recipients": ["x@y.com"]}).status_code == 401
        r = c.get("/")
        assert r.status_code == 401 and "Access token" in r.text, "shell must not render unauthenticated"

        assert c.post("/api/login", json={"token": "wrong"}).status_code == 401
        assert c.post("/api/login", json={"token": "s3cret"}).status_code == 200  # cookie now on the client
        assert c.get("/api/rules").status_code == 200
        assert c.get("/").status_code == 200

        # bearer works for API clients that cannot hold a cookie
        c.cookies.clear()
        assert c.get("/api/rules", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/api/rules", headers={"Authorization": "Bearer nope"}).status_code == 401
        c.post("/api/login", json={"token": "s3cret"})

        # unknown keys are rejected, so a stale client cannot silently write junk
        good = {"name": "t", "symbol": "AAPL", "timeframe": "5min",
                "condition": {"type": "rsi", "threshold": 15, "direction": "below"}}
        assert c.post("/api/rules", json=good).status_code == 200
        assert c.post("/api/rules", json={**good, "channels": ["sms"]}).status_code == 422
        # a symbol reaches an upstream URL path carrying our API key
        assert c.post("/api/rules", json={**good, "symbol": "../../etc"}).status_code == 422
        assert c.post("/api/rules", json={**good, "timeframe": "3min"}).status_code == 422
        assert c.get("/api/alerts?limit=99999").status_code == 422

        # the cron endpoint takes its OWN secret — the dashboard token must not work
        assert c.get("/api/cron/evaluate").status_code == 401
        assert c.get("/api/cron/evaluate", headers={"Authorization": "Bearer s3cret"}).status_code == 401
        engine.DEMO = True  # keep the cycle off the network
        try:
            assert c.get("/api/cron/evaluate", headers={"Authorization": "Bearer cr0n"}).status_code == 200
        finally:
            engine.DEMO = False

    config.AUTH_TOKEN = ""
    config.CRON_SECRET = ""
    config.DB_FILE = _TMP / "test.db"
    print("  ✓ api OK (cookie + bearer, shell gated, extra=forbid, symbol regex, cron secret)")


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


def main():
    print("=" * 60)
    print("Market Alert System — comprehensive tests (v2)")
    print("=" * 60)
    tests = [test_config, test_indicators, test_conditions, test_market_calendar, test_db,
             test_notifier_dry_run, test_engine_cycle,
             test_settings_recipients, test_backtest_and_watchlist, test_api_auth, test_ai]
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
