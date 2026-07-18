"""Comprehensive assert-based tests for the v2 (multi-indicator, email) system.

Run: python test_all.py   (no framework, no network — uses a temp SQLite db + demo series)
"""
import json
import sys
import tempfile
from pathlib import Path

import config

# Point persistence at a throwaway location BEFORE importing db/engine touch it.
_TMP = Path(tempfile.mkdtemp())
config.DB_FILE = _TMP / "test.db"
config.SNAPSHOT_FILE = _TMP / "snapshot.json"
config.DRY_RUN = True  # forces notifier into dry-run (logs, never sends)

import ai
import conditions
import db
import engine
import notifier
from rsi import wilder_rsi, sma, ema, percent_change


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

    up = {"closes": list(range(1, 40)), "volumes": []}
    down = {"closes": list(range(40, 1, -1)), "volumes": []}

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
                                     {"closes": [100, 105], "volumes": []})
    assert fired and val == 5.0

    fired, val = conditions.evaluate({"type": "ma_cross", "fast": 5, "slow": 20, "direction": "golden"}, up)
    assert fired and val > 0  # rising series → fast MA above slow MA

    # validation rejects bad input
    for bad in ({"type": "rsi"}, {"type": "nope", "x": 1}):
        try:
            conditions.validate(bad); raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass
    print("  ✓ conditions OK (rsi/rsi_band/price/percent_change/ma_cross + validation)")


def test_db():
    print("Testing db.py...")
    db.init()
    rule = db.create_rule({"id": "r1", "name": "t", "symbol": "AAPL", "timeframe": "5min",
                           "condition": {"type": "rsi", "threshold": 15, "direction": "below"},
                           "channels": [], "recipients": [], "enabled": True})
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

    db.append_alert(rule, "RSI below 15", 8.0, 150.0)
    assert len(db.list_alerts()) == 1
    db.delete_rule("r1")
    assert db.list_rules() == [] and db.get_rule("r1") is None
    print("  ✓ db OK (CRUD + fire-once/hold/reset/re-fire dedup)")


def test_notifier_dry_run():
    print("Testing notifier.py (email-only, dry-run)...")
    base = {"symbol": "AAPL", "timeframe": "5min", "name": "t", "channels": []}
    ts = "2026-07-09 10:00:00 ET"

    # empty channels -> email (the only/default channel)
    r = notifier.send(base, "RSI below 15", 12.3, 150.0, ts, dry_run=True)
    assert set(r) == {"email"} and r["email"]["sent"] is False
    assert r["email"]["subject"].endswith("RSI below 15")

    # explicit email channel -> email
    r = notifier.send({**base, "channels": ["email"]}, "RSI below 15", 12.3, 150.0, ts, dry_run=True)
    assert set(r) == {"email"}

    # only a retired channel -> nothing delivered
    r = notifier.send({**base, "channels": ["sms"]}, "RSI below 15", 12.3, 150.0, ts, dry_run=True)
    assert r == {}
    print("  ✓ notifier OK (email-only, dry-run, retired channels dropped)")


def test_engine_cycle():
    print("Testing engine.run_once (demo series, dedup)...")
    db.init()
    for r in db.list_rules():
        db.delete_rule(r["id"])
    # a rule that always fires (price below a huge level) → tests fire-once + snapshot
    db.create_rule({"id": "always", "name": "always", "symbol": "AAPL", "timeframe": "5min",
                    "condition": {"type": "price", "level": 1_000_000_000, "direction": "below"},
                    "channels": [], "recipients": [], "enabled": True})
    engine.run_once(series_provider=engine.demo_series)
    assert config.SNAPSHOT_FILE.exists()
    snap = json.loads(config.SNAPSHOT_FILE.read_text())
    assert snap["rules"] and snap["rules"][0]["fired"] is True
    assert snap["healthy"] is True and "last_success_ms" in snap, "snapshot missing dead-man heartbeat"
    n1 = len(db.list_alerts())
    engine.run_once(series_provider=engine.demo_series)  # still fired, not fresh
    assert len(db.list_alerts()) == n1, "dedup failed: alert re-fired while held"
    # alerts carry epoch-ms for the "triggered X ago" counter
    assert db.list_alerts()[0]["ts_ms"] and db.list_alerts()[0]["ts_ms"] > 0
    print(f"  ✓ engine OK (snapshot written, {n1} alert, no re-fire while held)")


def test_settings_recipients():
    print("Testing db settings + notifier recipient resolution...")
    db.init()
    config.DEFAULT_EMAIL_RECIPIENTS = ["fallback@x.com"]
    assert notifier._recipients() == ["fallback@x.com"]          # no setting → .env default
    db.set_setting("email_recipients", "a@x.com, b@y.com")
    assert notifier._recipients() == ["a@x.com", "b@y.com"]      # saved setting wins
    assert notifier._recipients({"email_recipients": ["r@z.com"]}) == ["r@z.com"]  # rule override wins
    print("  ✓ settings OK (rule > saved > .env recipient resolution)")


def test_backtest_and_watchlist():
    print("Testing engine.backtest + engine.watchlist (injected series)...")
    rising = {"closes": list(range(1, 60)), "volumes": [1.0] * 59}
    res = engine.backtest("AAPL", "5min", {"type": "rsi", "threshold": 90, "direction": "above"},
                          series_provider=lambda s, t: rising)
    assert res["bars"] == 59 and res["fires"] == 1, res  # RSI pegs 100 → fires once on the edge, no re-fire
    wl = engine.watchlist(series_provider=lambda s, t: {"closes": list(range(1, 40)), "volumes": []})
    assert len(wl) == len(config.SYMBOLS)
    assert wl[0]["price"] == 39 and wl[0]["rows"][0]["rsi"] == 100.0
    print(f"  ✓ backtest/watchlist OK (1 fire over 59 bars, {len(wl)} symbols)")


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
    tests = [test_config, test_indicators, test_conditions, test_db,
             test_notifier_dry_run, test_engine_cycle,
             test_settings_recipients, test_backtest_and_watchlist, test_ai]
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
