# Alert Radar

A customizable, multi-indicator market alert system for stocks and ETFs. Define your own rules — RSI, RSI band, price levels, percent change, moving-average crosses — and get an **email the moment one triggers**. Manage everything from a claymorphism web dashboard: live rules, watchlist, backtesting, history, and settings.

## Features

- **Customizable rules** — RSI, RSI band (fires on either bound), price, % change, MA cross. The condition registry is extensible (add a type in `conditions.py`).
- **Email delivery via [Resend](https://resend.com)** — HTML alerts from your verified domain. (SMS/WhatsApp/RCS were retired — email only.)
- **Live dashboard** — create/edit/pause/delete rules, live rule status, and a "triggered X ago" counter on every alert.
- **Watchlist** — live price + RSI across every watched symbol × timeframe.
- **Backtest** — replay any condition over recent bars to see how often it would have fired.
- **Dead-man's switch** — if the evaluation loop stops completing healthy cycles, you get an email that the monitor is down (and the dashboard shows a "Monitor stalled" badge).
- **Onboarding** — a short first-run flow to set your alert email and create a first rule.
- **Dry-run mode** — logs notifications instead of sending, for safe testing.
- **SQLite persistence** — rules, dedup state, alert history, and settings.

## Prerequisites

- **Python 3.11+**
- **[Massive](https://massive.com) account** (formerly Polygon.io) — a paid plan is required for real-time bars. Not needed for `--demo`.
- **[Resend](https://resend.com) account** — an API key and a **verified sending domain** (e.g. `syncsolutions.ai`) to email real recipients.

## Setup

```bash
cp .env.example .env      # then fill in the keys
pip install -r requirements.txt
```

Configure `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `MASSIVE_API_KEY` | Massive API key (real-time market data) | (empty) |
| `MASSIVE_BASE_URL` | Override the Massive API base | `https://api.massive.com` |
| `RESEND_API_KEY` | Resend API key | (empty) |
| `EMAIL_FROM` | Sender — must be on a Resend-verified domain | `Alert Radar <alerts@syncsolutions.ai>` |
| `DEFAULT_EMAIL_RECIPIENTS` | Fallback recipients, comma-separated (also editable in Settings) | (empty) |
| `DRY_RUN` | Log notifications instead of sending | `true` |
| `DASHBOARD_PORT` | Web dashboard port | `8000` |
| `DEADMAN_SECONDS` | Warn if no successful cycle within this window | `max(300, refresh×6)` |

> Recipients can also be set live from the dashboard **Settings → Alert recipients** (stored in the database; no restart needed). Resolution order per alert: rule override → saved Settings → `DEFAULT_EMAIL_RECIPIENTS`.

## Running

```bash
python main.py            # live: real Massive data + email delivery (respects DRY_RUN)
python main.py --demo     # synthetic data, no API keys needed, notifications dry-run
```

Dashboard: `http://localhost:8000` · API docs: `http://localhost:8000/docs`

The engine evaluates enabled rules every 30s during market hours (9:30–16:00 ET, Mon–Fri), deduplicates (fires once on the false→true edge, re-arms when the condition clears), and writes a snapshot the dashboard polls.

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/meta` | symbols, timeframes, channels, condition schema |
| GET | `/api/snapshot` | live rule status + recent alerts + health |
| GET/POST | `/api/rules` | list / create rules |
| PUT/DELETE | `/api/rules/{id}` | update / delete a rule |
| GET | `/api/alerts` | alert history |
| GET/PUT | `/api/settings` | read / set alert recipients |
| GET | `/api/watchlist` | price + RSI grid across symbols × timeframes |
| POST | `/api/backtest` | replay a condition over recent bars |

## Testing

```bash
python test_all.py        # no framework, no network — temp SQLite + synthetic series
```

## Deployment

### Docker

```bash
docker compose up -d      # loads .env, persists SQLite in ./data/
docker compose down
```

### Linux VPS (systemd)

```bash
sudo cp deploy/rsi-alerts.service /etc/systemd/system/
sudo mkdir -p /opt/rsi-alerts && sudo cp -r . /opt/rsi-alerts/
chmod 600 /opt/rsi-alerts/.env
sudo systemctl daemon-reload && sudo systemctl enable --now rsi-alerts
sudo journalctl -u rsi-alerts -f
```

Adjust paths/users in `deploy/rsi-alerts.service` to match your host.

### Windows

Run `python main.py` under Task Scheduler ("At startup") or NSSM (`nssm install AlertRadar "C:\path\to\python.exe" "main.py"`).

## Architecture

Single long-lived process (FastAPI + uvicorn) with a background evaluation thread:

```
main.py → FastAPI + eval-loop thread
  loop every REFRESH_SECONDS (market hours):
    enabled rules → massive_client.get_bars → indicators → conditions.evaluate
    → dedup (SQLite) → notifier.send (email/dry-run) → snapshot + history
    → dead-man's switch checks the heartbeat each cycle
```

- `config.py` — env + settings   · `massive_client.py` — Massive REST client
- `rsi.py` — pure-Python indicators   · `conditions.py` — extensible condition registry
- `db.py` — SQLite (rules, dedup state, alerts, settings)   · `notifier.py` — Resend email + dead-man switch
- `engine.py` — eval loop, snapshot, watchlist, backtest   · `index.html` — no-build SPA dashboard

> Not financial advice. Alert Radar reports indicator conditions you configure; it does not recommend trades.
