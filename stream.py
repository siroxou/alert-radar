"""Real-time bar stream from Alpaca (IEX) for the always-on deployment shape.

A serverless function cannot hold a websocket open, so this runs only under the
resident process (Docker / systemd). It keeps an in-memory series cache and hands
that cache to the engine as a `series_provider` — the seam the test suite and demo
mode already use. Evaluation, dedup, history, email and snapshots are therefore
completely unchanged; only where the numbers come from is different.

Two drivers share the cache, on purpose:

  bar arrival  -> evaluate immediately. This is the whole point: an alert lands
                  seconds after the bar closes instead of up to a poll interval later.
  periodic tick -> evaluate every REFRESH_SECONDS regardless. Without it the
                  dead-man heartbeat goes stale whenever the tape is quiet or the
                  market is shut, and the dashboard would read "monitor stalled"
                  every evening.

Backfill is REST (alpaca_client) because the stream only carries bars from the
moment you connect, and RSI(14) on 15-minute bars needs about four hours of
history before it can say anything at all.
"""
import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone

import websockets  # ships with uvicorn[standard]; not a new dependency

import alpaca_client
import config
import db
import engine

logger = logging.getLogger("rsi_alerts")

# (symbol, timeframe) -> list[float] closes, oldest first
_SERIES = {}
# (symbol, timeframe) -> [bucket_index, running_close] for the bar still forming
_PENDING = {}
_lock = threading.Lock()
_evaluating = threading.Event()

_TF_MINUTES = {"1min": 1, "5min": 5, "15min": 15, "1hour": 60}


def epoch_minute(ts):
    """Alpaca bar timestamp ("2026-08-17T13:35:00Z", always UTC) -> epoch minutes.

    Parsed as explicitly UTC. time.mktime() would read it as local time, and the
    usual `- time.timezone` correction ignores DST — which would silently shift
    every bucket boundary by an hour for half the year.
    """
    return int(datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp()) // 60


def provider(symbol, timeframe):
    """series_provider for the engine: pure memory, no network, no rate limit."""
    with _lock:
        closes = _SERIES.get((symbol, timeframe))
    if not closes:
        raise ValueError(f"no streamed series yet for {symbol} {timeframe}")
    return {"closes": list(closes)}


def wanted_pairs():
    """Distinct (symbol, timeframe) across every user's enabled rules."""
    return {(r["symbol"], r["timeframe"]) for r in db.all_enabled_rules()
            if r["timeframe"] in _TF_MINUTES}


def wanted_symbols(pairs):
    """Symbols to subscribe, bounded by the free feed's ceiling.

    Signup is open, so this set is attacker-influenced. Sorted before truncating
    so the cut is deterministic rather than dependent on set iteration order.
    """
    symbols = sorted({s for s, _tf in pairs})
    if len(symbols) > config.MAX_STREAM_SYMBOLS:
        dropped = symbols[config.MAX_STREAM_SYMBOLS:]
        # never silently truncate: rules on these symbols will simply never fire,
        # and that is invisible from the dashboard.
        logger.error("stream symbol cap %d exceeded — NOT subscribing to %s",
                     config.MAX_STREAM_SYMBOLS, ",".join(dropped))
        symbols = symbols[:config.MAX_STREAM_SYMBOLS]
    return symbols


def backfill(pairs):
    """REST-fill history for pairs we do not have yet. Free tier withholds the
    last ~15 minutes; the stream covers exactly that gap."""
    for symbol, tf in sorted(pairs):
        with _lock:
            if _SERIES.get((symbol, tf)):
                continue
        try:
            mult, span, _disp, lookback = config.TIMEFRAMES[tf]
            closes = alpaca_client.get_bars(symbol, mult, span, lookback)
            with _lock:
                _SERIES[(symbol, tf)] = closes[-alpaca_client.MAX_BARS:]
            logger.info("backfilled %s %s: %d bars", symbol, tf, len(closes))
        except Exception:
            logger.exception("backfill failed for %s %s", symbol, tf)


def apply_bar(symbol, epoch_minute, close):
    """Fold one streamed minute bar into every timeframe series for `symbol`.

    Alpaca emits a minute bar once that minute has closed, so minute `m` completes
    its N-minute bucket exactly when `(m + 1) % N == 0`. Checking completion that
    way rather than waiting for the next bucket's first bar is what keeps this
    lag-free — otherwise every timeframe would trail by one bar, which for 1hour
    would be an hour.
    """
    advanced = False
    with _lock:
        for tf, minutes in _TF_MINUTES.items():
            key = (symbol, tf)
            if key not in _SERIES:
                continue  # nothing backfilled → no rule wants it
            _PENDING[key] = [epoch_minute // minutes, close]
            if (epoch_minute + 1) % minutes == 0:
                _SERIES[key].append(close)
                del _SERIES[key][:-alpaca_client.MAX_BARS]
                _PENDING.pop(key, None)
                advanced = True
    return advanced


def _evaluate():
    """Run one engine cycle against the cached series."""
    if _evaluating.is_set():
        return  # a slow cycle must not stack behind itself
    _evaluating.set()
    try:
        engine.run_cycle(series_provider=provider)
    except Exception:
        logger.exception("streamed evaluation failed")
    finally:
        _evaluating.clear()


async def _pump(ws, loop):
    """Consume messages until the socket dies."""
    async for raw in ws:
        try:
            msgs = json.loads(raw)
        except ValueError:
            continue
        advanced = False
        for m in msgs if isinstance(msgs, list) else [msgs]:
            t = m.get("T")
            if t == "b":
                try:
                    epoch_min = epoch_minute(m.get("t", ""))
                except ValueError:
                    continue
                advanced |= apply_bar(m.get("S", ""), epoch_min, m.get("c"))
            elif t == "error":
                logger.error("alpaca stream error %s: %s", m.get("code"), m.get("msg"))
            elif t == "subscription":
                logger.info("subscribed to %d symbol(s)", len(m.get("bars") or []))
        if advanced:
            # off the event loop: a cycle does database writes and an email round
            # trip, and blocking here would stall the socket and drop bars.
            loop.run_in_executor(None, _evaluate)


async def _session():
    """One connect → auth → subscribe → pump lifetime."""
    pairs = wanted_pairs()
    if not pairs:
        logger.info("no enabled rules — nothing to stream")
        await asyncio.sleep(30)
        return
    backfill(pairs)
    symbols = wanted_symbols(pairs)
    if not symbols:
        await asyncio.sleep(30)
        return

    async with websockets.connect(config.ALPACA_STREAM_URL, ping_interval=20) as ws:
        await ws.recv()  # {"T":"success","msg":"connected"}
        await ws.send(json.dumps({"action": "auth", "key": config.ALPACA_API_KEY,
                                  "secret": config.ALPACA_API_SECRET}))
        reply = json.loads(await ws.recv())
        if not any(m.get("T") == "success" and m.get("msg") == "authenticated"
                   for m in (reply if isinstance(reply, list) else [reply])):
            raise RuntimeError(f"alpaca auth failed: {reply}")
        await ws.send(json.dumps({"action": "subscribe", "bars": symbols}))
        logger.info("streaming %d symbol(s) from %s", len(symbols), config.ALPACA_STREAM_URL)
        await _pump(ws, asyncio.get_running_loop())


async def _run():
    """Reconnect forever. A dropped socket is a stopped monitor, and the exchange
    day is long enough that one will drop."""
    backoff = 1
    while True:
        try:
            await _session()
            backoff = 1
        except Exception as exc:
            logger.warning("stream disconnected (%s); retrying in %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def _ticker():
    """Periodic cycle so the heartbeat stays fresh when the tape is quiet.

    Without this the dead-man's switch trips every evening and weekend: nothing
    writes a heartbeat because no bars arrive, and `healthy` is derived on read.
    """
    while True:
        time.sleep(config.REFRESH_SECONDS)
        try:
            pairs = wanted_pairs()
            if pairs:
                backfill(pairs)  # picks up symbols friends added since we connected
            _evaluate()
        except Exception:
            logger.exception("periodic tick failed")


def start_thread():
    threading.Thread(target=lambda: asyncio.run(_run()), daemon=True).start()
    threading.Thread(target=_ticker, daemon=True).start()
