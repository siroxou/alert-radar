import time as _time
from datetime import date, timedelta

import requests

import config

BASE_URL = config.MASSIVE_BASE_URL  # Massive (formerly Polygon.io); Polygon-compatible /v2/aggs shape
MAX_BARS = 250  # newest N bars — enough for RSI/%/MA up to ~200 period


def _get_with_retry(url, params, retries=1):
    """GET with a single retry on HTTP 429 (rate limit)."""
    for attempt in range(retries + 1):
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 429 and attempt < retries:
            _time.sleep(1.5)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def get_bars(symbol: str, multiplier: int, timespan: str, lookback_days: int) -> tuple[list[float], list[float]]:
    """Return (closes, volumes) oldest-first for the MOST RECENT bars.

    Uses sort=desc + limit so we always get the newest bars, then reverses to
    ascending. (sort=asc + limit would return the *oldest* bars in the window.)
    """
    to_date = date.today()
    from_date = to_date - timedelta(days=lookback_days)
    url = (
        f"{BASE_URL}/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}"
        f"/{from_date.isoformat()}/{to_date.isoformat()}"
    )
    params = {
        "adjusted": "true",
        "sort": "desc",
        "limit": MAX_BARS,
        "apiKey": config.MASSIVE_API_KEY,
    }
    resp = _get_with_retry(url, params)
    results = resp.json().get("results", [])
    if not results:
        raise ValueError(f"no bars returned for {symbol} {multiplier}{timespan}")

    results.reverse()  # desc -> ascending (oldest first)
    closes = [bar["c"] for bar in results]
    volumes = [bar.get("v", 0.0) for bar in results]
    return closes, volumes
