"""Email delivery via Resend.

SMS/WhatsApp/RCS (sent.dm) were retired — email is the only channel now.
DRY_RUN (default) or missing credentials/recipients → the payload is logged, not sent.
A rule delivers when its channels include "email" or are empty (the default).
"""
import logging
from html import escape

import requests

import config
import db

logger = logging.getLogger("rsi_alerts")
RESEND_URL = "https://api.resend.com/emails"


def _recipients(rule=None):
    """Resolve who gets the email: rule override → saved Settings → .env default."""
    if rule and rule.get("email_recipients"):
        return rule["email_recipients"]
    saved = db.get_setting("email_recipients")
    if saved:
        return [e.strip() for e in saved.split(",") if e.strip()]
    return config.DEFAULT_EMAIL_RECIPIENTS


def send(rule, description, value, price, timestamp, dry_run=None):
    dry = config.DRY_RUN if dry_run is None else dry_run
    channels = rule.get("channels") or []
    if "email" not in channels and channels:
        return {}  # rule targets only a retired channel → nothing to deliver
    return {"email": _send_email(rule, description, value, price, timestamp, dry)}


def send_deadman(reason, stale_seconds, dry_run=None):
    """Dead-man's switch: the eval loop hasn't completed a cycle in too long → warn ops."""
    dry = config.DRY_RUN if dry_run is None else dry_run
    recipients = _recipients()
    subject = "⚠️ Alert Radar monitor is DOWN — no successful cycle"
    if dry or not config.RESEND_API_KEY or not recipients:
        why = "DRY_RUN" if dry else "missing RESEND_API_KEY/recipients"
        logger.warning("[%s] DEAD-MAN would email → to=%s reason=%s stale=%ss", why, recipients, reason, stale_seconds)
        return {"sent": False, "to": recipients}
    resp = requests.post(RESEND_URL, timeout=10,
        headers={"Authorization": f"Bearer {config.RESEND_API_KEY}", "Content-Type": "application/json"},
        json={"from": config.EMAIL_FROM, "to": recipients, "subject": subject,
              "html": _deadman_html(reason, stale_seconds)})
    resp.raise_for_status()
    logger.warning("DEAD-MAN email sent → to=%s", recipients)
    return {"sent": True, "to": recipients}


def _send_email(rule, description, value, price, timestamp, dry):
    recipients = _recipients(rule)
    subject = f"\U0001F6A8 {rule['symbol']} {rule['timeframe']} — {description}"
    if dry or not config.RESEND_API_KEY or not recipients:
        reason = "DRY_RUN" if dry else "missing RESEND_API_KEY/recipients"
        logger.info("[%s] would email → from=%s to=%s subject=%s", reason, config.EMAIL_FROM, recipients, subject)
        return {"sent": False, "to": recipients, "subject": subject}
    resp = requests.post(RESEND_URL, timeout=10,
        headers={"Authorization": f"Bearer {config.RESEND_API_KEY}", "Content-Type": "application/json"},
        json={"from": config.EMAIL_FROM, "to": recipients, "subject": subject,
              "html": _email_html(rule, description, value, price, timestamp)})
    resp.raise_for_status()
    logger.info("email sent → to=%s", recipients)
    return {"sent": True, "to": recipients, "status": resp.status_code}


def _email_html(rule, description, value, price, timestamp):
    return f"""\
<div style="margin:0;padding:24px;background:#f2edfc;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#2d2a45">
  <div style="max-width:460px;margin:0 auto;background:#ffffff;border-radius:20px;padding:28px;box-shadow:0 8px 24px rgba(97,80,235,.14)">
    <div style="display:inline-block;background:#e9e4fe;color:#5646e5;font-weight:700;font-size:12px;padding:6px 12px;border-radius:999px;letter-spacing:.03em">&#128680; ALERT RADAR</div>
    <h1 style="margin:16px 0 4px;font-size:22px;font-weight:800">{escape(rule['symbol'])} &middot; {escape(rule['timeframe'])}</h1>
    <p style="margin:0 0 20px;color:#67628a;font-size:15px;font-weight:600">{escape(description)}</p>
    <table style="width:100%;border-collapse:collapse;font-size:14px">
      <tr><td style="padding:10px 0;color:#67628a;border-bottom:1px solid #f0edfb">Value</td><td style="padding:10px 0;text-align:right;font-weight:700;font-family:monospace">{value:.2f}</td></tr>
      <tr><td style="padding:10px 0;color:#67628a;border-bottom:1px solid #f0edfb">Price</td><td style="padding:10px 0;text-align:right;font-weight:700;font-family:monospace">${price:.2f}</td></tr>
      <tr><td style="padding:10px 0;color:#67628a">Time</td><td style="padding:10px 0;text-align:right;font-weight:700;font-family:monospace">{escape(timestamp)}</td></tr>
    </table>
    <p style="margin:22px 0 0;color:#9a94be;font-size:12px">Triggered by your rule &ldquo;{escape(rule['name'])}&rdquo;. Manage alerts in Alert Radar.</p>
  </div>
</div>"""


def _deadman_html(reason, stale_seconds):
    return f"""\
<div style="margin:0;padding:24px;background:#fff4f4;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#2d2a45">
  <div style="max-width:460px;margin:0 auto;background:#ffffff;border-radius:20px;padding:28px;box-shadow:0 8px 24px rgba(214,59,91,.16)">
    <div style="display:inline-block;background:#ffe1e4;color:#a62b47;font-weight:700;font-size:12px;padding:6px 12px;border-radius:999px;letter-spacing:.03em">&#9888; MONITOR DOWN</div>
    <h1 style="margin:16px 0 4px;font-size:22px;font-weight:800">Alert Radar stopped checking</h1>
    <p style="margin:0 0 20px;color:#67628a;font-size:15px;font-weight:600">No successful evaluation cycle in the last {int(stale_seconds)}s. Your rules are NOT being monitored right now.</p>
    <p style="margin:0;color:#67628a;font-size:14px"><strong>Last error:</strong> {escape(str(reason))}</p>
    <p style="margin:22px 0 0;color:#9a94be;font-size:12px">Check the host/process and market-data connectivity. This is an automated dead-man's switch.</p>
  </div>
</div>"""
