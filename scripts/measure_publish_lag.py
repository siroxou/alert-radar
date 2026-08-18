#!/usr/bin/env python3
"""Measure how long Alpaca actually takes to publish a closed bar.

config.BAR_PUBLISH_LAG_SECONDS decides how long a cron invocation sleeps before
evaluating, so the whole boundary-alignment win rests on that number. Today it is
an assertion in a docstring ("~15 SECONDS", alpaca_client.py:7) that nothing in the
repo measures. Set it from data instead.

    python3 scripts/measure_publish_lag.py            # 5 one-minute bars, SPY
    python3 scripts/measure_publish_lag.py NVDA 10

Run it DURING market hours — outside them no new bar ever closes and every sample
times out. Needs ALPACA_API_KEY / ALPACA_API_SECRET (it uses the app's own client).
"""
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import alpaca_client  # noqa: E402
import config  # noqa: E402
import engine  # noqa: E402

POLL_SECONDS = 1.0
GIVE_UP_AFTER = 90  # a bar that has not published in 90s is a data problem, not lag


def one_sample(symbol):
    """Wait for the next 1-minute close, then poll until that bar appears."""
    boundary = engine.next_boundary({"1min"})
    sleep_for = (boundary - datetime.now(engine.ET)).total_seconds()
    print(f"  waiting {sleep_for:5.1f}s for the {boundary:%H:%M} close…", flush=True)
    time.sleep(max(0, sleep_for))

    baseline = alpaca_client.get_bars(symbol, 1, "minute", 1)
    started = time.monotonic()
    while time.monotonic() - started < GIVE_UP_AFTER:
        time.sleep(POLL_SECONDS)
        try:
            bars = alpaca_client.get_bars(symbol, 1, "minute", 1)
        except Exception as e:  # transient upstream error — keep polling
            print(f"    fetch error ({e}); retrying", flush=True)
            continue
        # A new close appended (or the last value changed) means the bar landed.
        if len(bars) != len(baseline) or bars[-1] != baseline[-1]:
            return time.monotonic() - started
    return None


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    samples = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    if not alpaca_client.available():
        sys.exit("ALPACA_API_KEY / ALPACA_API_SECRET are not set")
    if not engine.market_is_open():
        print("WARNING: the market is closed — no bar will close, every sample will time out.\n")

    print(f"Measuring publish lag: {symbol}, {samples} one-minute bars "
          f"(feed={config.ALPACA_FEED})\n")
    lags = []
    for i in range(1, samples + 1):
        print(f"sample {i}/{samples}")
        lag = one_sample(symbol)
        if lag is None:
            print(f"  no bar within {GIVE_UP_AFTER}s — skipped\n")
            continue
        lags.append(lag)
        print(f"  bar published {lag:.1f}s after the close\n")

    if not lags:
        sys.exit("no samples collected — run during market hours on a liquid symbol")

    lags.sort()
    p50 = lags[len(lags) // 2]
    worst = lags[-1]
    print("=" * 56)
    print(f"samples={len(lags)}  min={lags[0]:.1f}s  p50={p50:.1f}s  max={worst:.1f}s")
    # Set the knob above the worst observed lag: sleeping a little too long costs a
    # couple of seconds, whereas waking too early reads the STALE bar and forfeits
    # the whole alignment win for that boundary.
    print(f"\nsuggested:  BAR_PUBLISH_LAG_SECONDS={int(worst) + 2}")
    print(f"currently:  BAR_PUBLISH_LAG_SECONDS={config.BAR_PUBLISH_LAG_SECONDS}")
    if config.BAR_PUBLISH_LAG_SECONDS < worst:
        print("\n  ^ the current value is BELOW the worst observed lag: some boundaries")
        print("    will read the previous bar and fall back to the next ordinary ping.")


if __name__ == "__main__":
    main()
