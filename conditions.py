"""Extensible alert-condition registry.

Each condition is a level/zone predicate: evaluate() returns (fired, value) for the
CURRENT bar. Dedup (fire once on the False->True edge, reset when it clears) is handled
in db.record_transition — so even an "MA cross" is modeled as "fast is now above slow".

Add a new condition type by subclassing Condition and decorating with @register("name").
"""
import rsi as indicators

REGISTRY = {}


def register(type_name):
    def deco(cls):
        cls.type = type_name
        REGISTRY[type_name] = cls()
        return cls
    return deco


class Condition:
    type = ""
    required = ()

    def validate(self, params):
        missing = [k for k in self.required if k not in params or params[k] in ("", None)]
        if missing:
            raise ValueError(f"{self.type} condition missing params: {missing}")

    def evaluate(self, series, params):
        raise NotImplementedError

    def describe(self, params):
        raise NotImplementedError


@register("rsi")
class RSICondition(Condition):
    required = ("threshold", "direction")

    def evaluate(self, series, params):
        period = int(params.get("period", 14))
        value = indicators.wilder_rsi(series["closes"], period)
        thr = float(params["threshold"])
        fired = value < thr if params["direction"] == "below" else value > thr
        return fired, value

    def describe(self, params):
        return f"RSI({int(params.get('period', 14))}) {params['direction']} {params['threshold']}"


@register("rsi_band")
class RSIBandCondition(Condition):
    """Fires when RSI leaves the low–high band (i.e. hits either bound)."""
    required = ("low", "high")

    def evaluate(self, series, params):
        period = int(params.get("period", 14))
        value = indicators.wilder_rsi(series["closes"], period)
        fired = value < float(params["low"]) or value > float(params["high"])
        return fired, value

    def describe(self, params):
        return f"RSI({int(params.get('period', 14))}) hits {params['low']} or {params['high']}"


@register("price")
class PriceCondition(Condition):
    required = ("level", "direction")

    def evaluate(self, series, params):
        value = series["closes"][-1]
        lvl = float(params["level"])
        fired = value < lvl if params["direction"] == "below" else value > lvl
        return fired, value

    def describe(self, params):
        return f"Price {params['direction']} ${params['level']}"


@register("percent_change")
class PercentChangeCondition(Condition):
    required = ("pct", "direction")

    def evaluate(self, series, params):
        bars = int(params.get("bars", 1))
        value = indicators.percent_change(series["closes"], bars)
        pct = float(params["pct"])
        fired = value <= -pct if params["direction"] == "down" else value >= pct
        return fired, value

    def describe(self, params):
        return f"{params['direction']} {params['pct']}% over {int(params.get('bars', 1))} bar(s)"


@register("ma_cross")
class MACrossCondition(Condition):
    required = ("fast", "slow", "direction")

    def evaluate(self, series, params):
        kind = params.get("kind", "sma")
        fast, slow = int(params["fast"]), int(params["slow"])
        ma = indicators.ema if kind == "ema" else indicators.sma
        spread = ma(series["closes"], fast) - ma(series["closes"], slow)
        fired = spread > 0 if params["direction"] == "golden" else spread < 0
        return fired, spread

    def describe(self, params):
        return f"{params.get('kind', 'sma').upper()} {params['fast']}/{params['slow']} {params['direction']} cross"


# --- module-level helpers used by the engine + API ---

def _params(condition):
    return {k: v for k, v in condition.items() if k != "type"}


def validate(condition):
    t = condition.get("type")
    if t not in REGISTRY:
        raise ValueError(f"unknown condition type: {t!r} (known: {list(REGISTRY)})")
    REGISTRY[t].validate(_params(condition))


def evaluate(condition, series):
    return REGISTRY[condition["type"]].evaluate(series, _params(condition))


def describe(condition):
    return REGISTRY[condition["type"]].describe(_params(condition))


def min_bars(condition):
    """Conservative minimum bar count a condition needs."""
    p = condition
    t = condition["type"]
    if t == "rsi":
        return int(p.get("period", 14)) + 1
    if t == "ma_cross":
        return int(p.get("slow", 50)) + 1
    if t == "percent_change":
        return int(p.get("bars", 1)) + 1
    return 1


def schema():
    """Form schema for the dashboard's dynamic Create/Edit rule form."""
    return [
        {"type": "rsi", "label": "RSI", "params": [
            {"name": "period", "type": "number", "default": 14},
            {"name": "threshold", "type": "number", "default": 30},
            {"name": "direction", "type": "select", "options": ["above", "below"], "default": "below"}]},
        {"type": "rsi_band", "label": "RSI Band (hits either bound)", "params": [
            {"name": "period", "type": "number", "default": 14},
            {"name": "low", "type": "number", "default": 20},
            {"name": "high", "type": "number", "default": 80}]},
        {"type": "price", "label": "Price", "params": [
            {"name": "level", "type": "number", "default": 100},
            {"name": "direction", "type": "select", "options": ["above", "below"], "default": "above"}]},
        {"type": "percent_change", "label": "% Change", "params": [
            {"name": "pct", "type": "number", "default": 5},
            {"name": "bars", "type": "number", "default": 1},
            {"name": "direction", "type": "select", "options": ["up", "down"], "default": "up"}]},
        {"type": "ma_cross", "label": "MA Cross", "params": [
            {"name": "fast", "type": "number", "default": 9},
            {"name": "slow", "type": "number", "default": 21},
            {"name": "kind", "type": "select", "options": ["sma", "ema"], "default": "sma"},
            {"name": "direction", "type": "select", "options": ["golden", "death"], "default": "golden"}]},
    ]
