"""Market-data client for Alpaca.

Replaces Massive, whose free tier serves END-OF-DAY data only — during a live
session it returns bars through the previous close, so every rule would evaluate
yesterday's prices and latch a meaningless alert.

Alpaca's free tier serves REAL-TIME IEX here, not delayed data — measured ~15
SECONDS between a bar closing and this endpoint returning it. The widely-cited
15-MINUTE delay on Alpaca's free plan applies to the SIP feed; IEX has none.
That is why polling this on a ~1-minute cron is a complete solution on its own,
and why stream.py (websocket, ~5s, but capped at 30 symbols) is optional.
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

    # end is left unset on purpose: unbounded means "through the newest bar",
    # which on IEX is the bar that closed seconds ago. Naming an end would only
    # ever cut the series short of live.
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


def latest_prices(symbols) -> dict:
    """Current trade price per symbol — no waiting for a bar to close.

    get_bars only ever returns CLOSED bars, so on its own a move is invisible until
    its bar ends (up to 15 minutes on the default rules). This is the live tape:
    IEX last-trade, real-time on the free plan, same as the bars feed.

    Multi-symbol on purpose. One request covers the whole cycle's universe, so
    enabling live evaluation costs a single extra call per cycle rather than one
    per symbol — which matters because a cycle already fans out over every distinct
    symbol x timeframe pair.

    Never raises: a missing or failed price means that symbol falls back to
    closed-bar evaluation, which is exactly the old behaviour.
    """
    symbols = sorted({s for s in symbols if SYMBOL_RE.fullmatch(s or "")})
    if not symbols or not available():
        return {}
    try:
        resp = requests.get(f"{BASE_URL}/trades/latest",
                            params={"symbols": ",".join(symbols), "feed": config.ALPACA_FEED},
                            headers=_headers(), timeout=10)
        resp.raise_for_status()
        trades = resp.json().get("trades") or {}
    except Exception:
        return {}
    out = {}
    for sym, trade in trades.items():
        price = (trade or {}).get("p")
        if isinstance(price, (int, float)) and price > 0:
            out[sym] = float(price)
    return out
