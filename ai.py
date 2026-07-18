"""Local, in-process AI (optional) — powered by Qwen2.5-Instruct via llama-cpp-python.

Two features:
  - parse_rule(text): turn plain English into a validated alert rule (JSON-schema-constrained
    decoding guarantees valid JSON; the caller still runs conditions.validate()).
  - summarize_insights(stats): a 1-2 sentence natural-language read on recent alert activity.

Everything FAILS OPEN: if llama-cpp-python / huggingface_hub aren't installed, or the model can't be
downloaded/loaded, the callers raise AIUnavailable and the app runs exactly as it does without AI.
The GGUF is auto-downloaded to config.MODELS_DIR on first use (no server, no API key).
"""
from __future__ import annotations

import json
import logging

import config
import conditions

logger = logging.getLogger("rsi_alerts")

try:
    from llama_cpp import Llama
except Exception:  # not installed, or wheel unavailable on this platform
    Llama = None

try:
    from huggingface_hub import hf_hub_download
except Exception:
    hf_hub_download = None


class AIUnavailable(Exception):
    """Raised when local inference can't run — callers translate this to a graceful 503."""


_llm = None
_load_failed = False


def available() -> bool:
    """Cheap capability check (no download) used to gate the UI."""
    return bool(config.AI_ENABLED and Llama and hf_hub_download and not _load_failed)


def _get_llm():
    """Lazy singleton: download the GGUF on first use, then keep the model resident."""
    global _llm, _load_failed
    if _llm is not None:
        return _llm
    if _load_failed:
        raise AIUnavailable("model previously failed to load")
    if not (Llama and hf_hub_download):
        _load_failed = True
        raise AIUnavailable("llama-cpp-python / huggingface_hub not installed")
    try:
        config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("loading local AI model %s (%s) — first run downloads it",
                    config.AI_MODEL_REPO, config.AI_MODEL_FILE)
        path = hf_hub_download(repo_id=config.AI_MODEL_REPO, filename=config.AI_MODEL_FILE,
                               local_dir=str(config.MODELS_DIR))
        _llm = Llama(model_path=path, n_ctx=2048, verbose=False)
        logger.info("local AI model ready")
        return _llm
    except Exception as e:
        _load_failed = True
        raise AIUnavailable(f"could not load model: {e}")


# --- prompt + schema are derived from the live condition registry so they never drift ---

def _rule_schema() -> dict:
    """JSON schema for a rule draft, built from conditions.schema(). `condition` is a per-type
    oneOf: the grammar forces the model to pick one type and emit exactly that type's params (with
    enum-constrained selects), so it can't omit `direction` or invent wrong param names."""
    variants = []
    for c in conditions.schema():
        props = {"type": {"const": c["type"]}}
        for p in c["params"]:
            props[p["name"]] = {"type": "string", "enum": p["options"]} if p["type"] == "select" else {"type": "number"}
        variants.append({"type": "object", "properties": props,
                         "required": ["type"] + [p["name"] for p in c["params"]],
                         "additionalProperties": False})
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "symbol": {"type": "string"},
            "timeframe": {"type": "string", "enum": list(config.TIMEFRAMES)},
            "condition": {"oneOf": variants},
        },
        "required": ["symbol", "timeframe", "condition"],
    }


def _system_prompt() -> str:
    lines = [
        "You convert a trader's plain-English request into a single JSON alert rule.",
        "Output ONLY the JSON object — no prose, no markdown.",
        f"Symbols are uppercase tickers (examples in use: {', '.join(config.SYMBOLS)}).",
        "Timeframe must be one of these VALUES (not the label):",
    ]
    for k, v in config.TIMEFRAMES.items():
        lines.append(f'  "{k}"  = {v[2]}')
    lines.append("Condition types and their parameters:")
    for c in conditions.schema():
        parts = []
        for p in c["params"]:
            if p["type"] == "select":
                parts.append(f'{p["name"]} = one of {"/".join(p["options"])}')
            else:
                parts.append(f'{p["name"]} = number (default {p.get("default")})')
        lines.append(f'  "{c["type"]}" ({c["label"]}): {", ".join(parts)}')
    lines.append('Use ONLY the parameter names listed for the chosen type — e.g. percent_change '
                 'uses "direction" (up/down), NOT "kind"; "kind" belongs to ma_cross only.')
    lines.append('Also add a short human "name" for the rule.')
    return "\n".join(lines)


_FEWSHOT = [
    {"role": "user", "content": "email me when NVDA 5 minute RSI drops below 15"},
    {"role": "assistant", "content": '{"name": "NVDA 5m RSI oversold", "symbol": "NVDA", "timeframe": "5min", "condition": {"type": "rsi", "period": 14, "threshold": 15, "direction": "below"}}'},
    {"role": "user", "content": "alert me if SPY goes above 600 dollars on the 1 hour chart"},
    {"role": "assistant", "content": '{"name": "SPY above 600", "symbol": "SPY", "timeframe": "1hour", "condition": {"type": "price", "level": 600, "direction": "above"}}'},
    {"role": "user", "content": "tell me if AMZN jumps more than 3 percent in an hour"},
    {"role": "assistant", "content": '{"name": "AMZN +3% / 1h", "symbol": "AMZN", "timeframe": "1hour", "condition": {"type": "percent_change", "pct": 3, "bars": 1, "direction": "up"}}'},
    {"role": "user", "content": "notify me on a golden cross of the 9 and 21 EMA on QQQ 15 minute"},
    {"role": "assistant", "content": '{"name": "QQQ EMA golden cross", "symbol": "QQQ", "timeframe": "15min", "condition": {"type": "ma_cross", "fast": 9, "slow": 21, "kind": "ema", "direction": "golden"}}'},
]


def parse_rule(text: str) -> dict:
    """Plain English -> rule draft {name, symbol, timeframe, condition}. Not validated/saved here."""
    llm = _get_llm()
    messages = [{"role": "system", "content": _system_prompt()}, *_FEWSHOT,
                {"role": "user", "content": text}]
    try:
        resp = llm.create_chat_completion(
            messages=messages,
            response_format={"type": "json_object", "schema": _rule_schema()},
            temperature=0.1, max_tokens=192,
        )
        data = json.loads(resp["choices"][0]["message"]["content"])
    except AIUnavailable:
        raise
    except Exception as e:
        raise AIUnavailable(f"rule generation failed: {e}")
    data["symbol"] = str(data.get("symbol", "")).upper().strip()
    if not data.get("name"):
        cond = data.get("condition", {})
        data["name"] = f'{data["symbol"]} {data.get("timeframe", "")} {cond.get("type", "")}'.strip()
    return data


def summarize_insights(stats: dict) -> str:
    """A short, specific natural-language read on recent alert activity."""
    llm = _get_llm()
    try:
        resp = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": "You are a concise market-alert analyst. Given JSON "
                 "stats about which alerts fired recently, write 1-2 short, specific sentences a "
                 "trader would find useful. No preamble, no markdown."},
                {"role": "user", "content": json.dumps(stats)},
            ],
            temperature=0.4, max_tokens=160,
        )
        return resp["choices"][0]["message"]["content"].strip()
    except AIUnavailable:
        raise
    except Exception as e:
        raise AIUnavailable(f"summary failed: {e}")
