"""Market-data client for Alpaca.

Replaces Massive, whose free tier serves END-OF-DAY data only — during a live
session it returns bars through the previous close, so every rule would evaluate
yesterday's prices and latch a meaningless alert.

Alpaca's free tier splits the job in two, and this module handles the first half:

  REST  (here)      history up to ~15 minutes ago. Free tier withholds the most
                    recent 15 minutes, which is fine for backfill: RSI(14) on
                    15-minute bars needs ~4 hours of history, not the last tick.
  websocket (stream.py)  the live tail, real-time from IEX.

Together they give a complete, current series for free. Neither half does on its own.
"""
import re
from datetime import datetime, timedelta, timezone

import requests

import config

BASE_URL = "https://data.alpaca.markets/v2/stocks"
MAX_BARS = 250  # newest N bars — enough for RSI/%/MA up to ~200 period
# The symbol is interpolated into the request PATH with our credentials attached,
# so it is validated here — the one place every caller funnels through.
SYMBOL_RE = re.compile(r"[A-Z][A-Z.\-]{0,9}")

# (multiplier, timespan) -> Alpaca timeframe string. Keyed off config.TIMEFRAMES
# so a new entry there fails loudly here rather than silently fetching the wrong
# resolution.
_TIMEFRAME = {
    (1, "minute"): "1Min",
    (5, "minute"): "5Min",
    (15, "minute"): "15Min",
    (1, "hour"): "1Hour",
}


def _headers():
    return {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_API_SECRET,
    }


def available():
    return bool(config.ALPACA_API_KEY and config.ALPACA_API_SECRET)


def get_bars(symbol: str, multiplier: int, timespan: str, lookback_days: int) -> list:
    """Return closes oldest-first for the most recent bars.

    Same signature as the old Massive client so engine._fetch_series is unchanged.
    """
    if not SYMBOL_RE.fullmatch(symbol or ""):
        raise ValueError(f"invalid symbol: {symbol!r}")
    tf = _TIMEFRAME.get((multiplier, timespan))
    if tf is None:
        raise ValueError(f"unsupported timeframe: {multiplier}{timespan}")
    if not available():
        raise ValueError("ALPACA_API_KEY / ALPACA_API_SECRET are not set")

    # end is left unset on purpose: the free feed already withholds the last 15
    # minutes, and naming an end inside that window returns an error rather than
    # a short series.
    start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    params = {
        "timeframe": tf,
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": MAX_BARS,
        "adjustment": "raw",
        "feed": config.ALPACA_FEED,
        "sort": "desc",  # newest first, so `limit` keeps the NEWEST bars, not the oldest
    }
    resp = requests.get(f"{BASE_URL}/{symbol}/bars", params=params,
                        headers=_headers(), timeout=15)
    if resp.status_code == 401:
        raise ValueError("Alpaca rejected the credentials (401)")
    resp.raise_for_status()
    bars = resp.json().get("bars") or []
    if not bars:
        raise ValueError(f"no bars returned for {symbol} {tf}")
    bars.reverse()  # desc -> ascending (oldest first)
    return [b["c"] for b in bars]
