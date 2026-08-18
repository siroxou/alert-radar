#!/usr/bin/env python3
"""Measure how long Alpaca actually takes to publish a closed bar.

config.BAR_PUBLISH_LAG_SECONDS decides how long a cron invocation sleeps before
evaluating, so the whole boundary-alignment win rests on that number. Today it is
an assertion in a docstring ("~15 SECONDS", alpaca_client.py:7) that nothing in the
repo measures. Set it from data instead.

    python3 scripts/measure_publish_lag.py            # 10 one-minute bars, SPY
    python3 scripts/measure_publish_lag.py NVDA 5

RUN IT DURING REGULAR HOURS (09:30-16:00 ET). IEX is one venue at ~2.5% of volume
and prints very little pre/post-market, so outside RTH a one-minute bar frequently
does not exist at all and every sample times out.

This talks to the bars endpoint directly rather than through alpaca_client.get_bars,
because get_bars returns closes only (alpaca_client.py:82) and discards the bar
timestamps — and the timestamp is the only unambiguous way to tell that the bar you
were waiting for has actually landed. Comparing close PRICES silently misses a new
bar whenever two consecutive bars print the same close, which is common when volume
is thin — exactly the condition this script runs in.
"""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

import alpaca_client  # noqa: E402
import config  # noqa: E402
import engine  # noqa: E402

POLL_SECONDS = 1.0
GIVE_UP_AFTER = 90  # a bar this late is a data-availability problem, not publish lag


def newest_bar(symbol):
    """The most recent 1-minute bar as (start_datetime_utc, close), or None."""
    resp = requests.get(
        f"{alpaca_client.BASE_URL}/{symbol}/bars",
        params={"timeframe": "1Min", "limit": 1, "sort": "desc",
                "adjustment": "raw", "feed": config.ALPACA_FEED,
                "start": (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        headers=alpaca_client._headers(), timeout=15)
    resp.raise_for_status()
    bars = resp.json().get("bars") or []
    if not bars:
        return None
    b = bars[0]
    return datetime.fromisoformat(b["t"].replace("Z", "+00:00")), b["c"]


def one_sample(symbol):
    """Wait for the next 1-minute close, then poll until THAT bar appears."""
    close_at = engine.next_boundary({"1min"})
    # Alpaca stamps a bar with the START of its interval, so the bar that closes
    # at 09:46:00 is labelled 09:45:00. Wait for that label, not the close time.
    want = (close_at - timedelta(minutes=1)).astimezone(timezone.utc)

    sleep_for = (close_at - datetime.now(engine.ET)).total_seconds()
    print(f"  waiting {sleep_for:5.1f}s for the {close_at:%H:%M} close "
          f"(bar stamped {want:%H:%M}Z)…", flush=True)
    time.sleep(max(0, sleep_for))

    started = time.monotonic()
    last_seen = None
    while time.monotonic() - started < GIVE_UP_AFTER:
        try:
            bar = newest_bar(symbol)
        except Exception as e:
            print(f"    fetch error ({e}); retrying", flush=True)
            time.sleep(POLL_SECONDS)
            continue
        if bar is not None:
            last_seen = bar[0]
            if bar[0] >= want:
                return time.monotonic() - started, bar
        time.sleep(POLL_SECONDS)
    return None, last_seen


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    samples = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    if not alpaca_client.available():
        sys.exit("ALPACA_API_KEY / ALPACA_API_SECRET are not set")

    now = datetime.now(engine.ET)
    if not engine.market_is_open(now):
        print(f"WARNING: it is {now:%H:%M} ET and regular hours are 09:30-16:00.")
        print("         IEX prints almost nothing outside RTH, so bars frequently do not")
        print("         exist and samples will time out. Re-run during the session.\n")

    print(f"Measuring publish lag: {symbol}, {samples} one-minute bars "
          f"(feed={config.ALPACA_FEED})\n")
    lags, misses = [], 0
    for i in range(1, samples + 1):
        print(f"sample {i}/{samples}")
        lag, info = one_sample(symbol)
        if lag is None:
            misses += 1
            seen = f"newest bar is {info:%H:%M}Z" if info else "no bars at all"
            print(f"  no bar within {GIVE_UP_AFTER}s — skipped ({seen})\n")
            continue
        lags.append(lag)
        # Print the bar's own timestamp, not its price: if a minute produced no
        # prints at all the loop settles on a LATER bar, and only the timestamp
        # makes that visible instead of silently reporting it as this bar's lag.
        print(f"  bar {info[0]:%H:%M}Z published {lag:.1f}s after the close\n")

    print("=" * 60)
    if not lags:
        print(f"no samples collected ({misses} timed out).")
        print("If you ran this outside 09:30-16:00 ET, that is the reason: no bar closed.")
        sys.exit(1)

    lags.sort()
    p50, worst = lags[len(lags) // 2], lags[-1]
    print(f"samples={len(lags)}  timed_out={misses}  "
          f"min={lags[0]:.1f}s  p50={p50:.1f}s  max={worst:.1f}s")
    # Set the knob ABOVE the worst observed lag: sleeping a little too long costs a
    # couple of seconds, whereas waking too early reads the STALE bar and forfeits
    # the whole alignment win for that boundary.
    print(f"\nsuggested:  BAR_PUBLISH_LAG_SECONDS={int(worst) + 2}")
    print(f"currently:  BAR_PUBLISH_LAG_SECONDS={config.BAR_PUBLISH_LAG_SECONDS}")
    if config.BAR_PUBLISH_LAG_SECONDS < worst:
        print("\n  ^ the current value is BELOW the worst observed lag: those boundaries")
        print("    read the previous bar and fall back to the next ordinary ping.")


if __name__ == "__main__":
    main()
