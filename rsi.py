"""Pure-Python technical indicators (no numpy/pandas). Series are oldest-first."""


def wilder_rsi(closes: list[float], period: int = 14) -> float:
    """Wilder-smoothed RSI over a list of closes (oldest first)."""
    if len(closes) < period + 1:
        raise ValueError(f"need at least {period + 1} closes, got {len(closes)}")

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    gains = [max(d, 0.0) for d in deltas[:period]]
    losses = [max(-d, 0.0) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for d in deltas[period:]:
        gain = max(d, 0.0)
        loss = max(-d, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def sma(values: list[float], period: int) -> float:
    """Simple moving average of the last `period` values."""
    if len(values) < period:
        raise ValueError(f"need at least {period} values, got {len(values)}")
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> float:
    """Exponential moving average (seeded with the SMA of the first `period` values)."""
    if len(values) < period:
        raise ValueError(f"need at least {period} values, got {len(values)}")
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def percent_change(closes: list[float], bars: int) -> float:
    """Percent change of the latest close vs the close `bars` bars ago."""
    if bars < 1:
        raise ValueError("bars must be >= 1")
    if len(closes) < bars + 1:
        raise ValueError(f"need at least {bars + 1} closes, got {len(closes)}")
    ref = closes[-1 - bars]
    if ref == 0:
        return 0.0
    return (closes[-1] - ref) / ref * 100.0
