import os
from pathlib import Path

ROOT = Path(__file__).parent


def _load_dotenv(path=ROOT / ".env"):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split("#", 1)[0]  # strip inline comment
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def _env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- Market data (Massive — formerly Polygon.io. api.polygon.io still resolves, but we use Massive.) ---
MASSIVE_API_KEY = os.environ.get("MASSIVE_API_KEY", "")
MASSIVE_BASE_URL = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com")
DRY_RUN = _env_bool("DRY_RUN", True)
# sent.dm (SMS/WhatsApp/RCS) was retired — email is the only channel now.

# --- Email (Resend) ---
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
# syncsolutions.ai is the verified sending domain — deliver from an address on it.
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Alert Radar <alerts@syncsolutions.ai>")
DEFAULT_EMAIL_RECIPIENTS = [e.strip() for e in os.environ.get("DEFAULT_EMAIL_RECIPIENTS", "").split(",") if e.strip()]

# --- Server ---
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", 8000))
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", 30))
# dead-man's switch: email an ops warning if no successful eval cycle lands within this window
DEADMAN_SECONDS = int(os.environ.get("DEADMAN_SECONDS", max(300, REFRESH_SECONDS * 6)))

# --- Market universe used to seed starter rules / demo (rules can use any symbol) ---
SYMBOLS = ["AAPL", "NVDA", "AMZN", "IWM", "QQQ", "SPY"]

# label -> (multiplier, timespan, display, lookback_days)
TIMEFRAMES = {
    "1min": (1, "minute", "1-Minute", 3),
    "5min": (5, "minute", "5-Minute", 7),
    "15min": (15, "minute", "15-Minute", 14),
    "1hour": (1, "hour", "1-Hour", 40),
}

RSI_PERIOD = 14
OVERSOLD = int(os.environ.get("OVERSOLD", 15))
OVERBOUGHT = int(os.environ.get("OVERBOUGHT", 85))

# --- Persistence ---
DB_FILE = ROOT / "data" / "market_alerts.db"  # dir aligns with the docker-compose volume
SNAPSHOT_FILE = ROOT / "snapshot.json"
LOG_FILE = ROOT / "alerts.log"

# --- Local AI (optional, in-process via llama-cpp-python) ---
# Qwen2.5-1.5B-Instruct (Apache-2.0) — small, fast, strong at structured JSON. The GGUF is
# auto-downloaded to MODELS_DIR on first use; everything fails open if the model/lib is absent.
AI_ENABLED = _env_bool("AI_ENABLED", True)
AI_MODEL_REPO = os.environ.get("AI_MODEL_REPO", "Qwen/Qwen2.5-1.5B-Instruct-GGUF")
AI_MODEL_FILE = os.environ.get("AI_MODEL_FILE", "qwen2.5-1.5b-instruct-q4_k_m.gguf")
MODELS_DIR = ROOT / "models"
