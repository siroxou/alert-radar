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


# --- Runtime shape ---------------------------------------------------------
# Vercel (and any serverless host) gives us a read-only filesystem, no background
# threads, and a fresh process per request. SERVERLESS flips the app from
# "resident loop writing files" to "cron-driven, all state in Postgres".
SERVERLESS = bool(os.environ.get("VERCEL"))

# --- Market data (Massive — formerly Polygon.io. api.polygon.io still resolves, but we use Massive.) ---
MASSIVE_API_KEY = os.environ.get("MASSIVE_API_KEY", "")
MASSIVE_BASE_URL = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com")
DRY_RUN = _env_bool("DRY_RUN", True)

# --- Email (Resend) ---
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
# syncsolutions.ai is the verified sending domain — deliver from an address on it.
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Alert Radar <alerts@syncsolutions.ai>")
DEFAULT_EMAIL_RECIPIENTS = [e.strip() for e in os.environ.get("DEFAULT_EMAIL_RECIPIENTS", "").split(",") if e.strip()]

# --- Auth -----------------------------------------------------------------
# One shared password gates the dashboard and the API. The browser trades it for
# an httpOnly cookie (never readable by the Tailwind CDN script or by XSS); API
# clients may send it as `Authorization: Bearer <token>` instead.
# CRON_SECRET is deliberately a SEPARATE secret: Vercel Cron sends it, and a
# dashboard user must not be able to force evaluation cycles.
AUTH_TOKEN = os.environ.get("ALERT_RADAR_TOKEN", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
SESSION_COOKIE = "ar_session"
# ponytail: one shared password, no user table. Add per-user auth when there are users.

# --- Server ---
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", 8000))
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", 30))
# Dead-man's switch: warn if no successful eval cycle lands within this window.
# Serverless needs a much wider default — a cold start plus 24 sequential market
# fetches can easily outrun a 30s budget, and cron precision is not exact.
DEADMAN_SECONDS = int(os.environ.get("DEADMAN_SECONDS", 900 if SERVERLESS else max(300, REFRESH_SECONDS * 6)))

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

# --- Persistence ----------------------------------------------------------
# DATABASE_URL set  -> PostgreSQL (the only option that survives a read-only,
#                      per-request-process host like Vercel).
# DATABASE_URL unset -> SQLite on local disk, exactly as before.
DATABASE_URL = os.environ.get("DATABASE_URL", "") or os.environ.get("POSTGRES_URL", "")
STATE_DIR = Path(os.environ.get("STATE_DIR", "/tmp" if SERVERLESS else str(ROOT)))
DB_FILE = STATE_DIR / "data" / "market_alerts.db"  # dir aligns with the docker-compose volume
LOG_FILE = STATE_DIR / "alerts.log"

# --- Local AI (optional, in-process via llama-cpp-python) ---
# Qwen2.5-1.5B-Instruct (Apache-2.0) — small, fast, strong at structured JSON. The GGUF is
# auto-downloaded to MODELS_DIR on first use; everything fails open if the model/lib is absent.
# Not installed on Vercel (the wheel + a 1GB GGUF blow the function budget), so the AI
# surfaces simply report unavailable there and the dashboard hides them.
AI_ENABLED = _env_bool("AI_ENABLED", True) and not SERVERLESS
AI_MODEL_REPO = os.environ.get("AI_MODEL_REPO", "Qwen/Qwen2.5-1.5B-Instruct-GGUF")
AI_MODEL_FILE = os.environ.get("AI_MODEL_FILE", "qwen2.5-1.5b-instruct-q4_k_m.gguf")
MODELS_DIR = ROOT / "models"
