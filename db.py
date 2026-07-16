"""SQLite persistence for alert rules, per-rule fired-state (dedup), and alert history.

Concurrency-safe: WAL mode lets the eval-loop thread and the web CRUD thread coexist;
a lock serializes writes. Connections are per-call and closed promptly.
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import config

ET = ZoneInfo("America/New_York")
_write_lock = threading.Lock()


@contextmanager
def _db(write=False):
    conn = sqlite3.connect(config.DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if write:
            _write_lock.acquire()
        yield conn
        if write:
            conn.commit()
    finally:
        if write:
            _write_lock.release()
        conn.close()


def init():
    config.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _db(write=True) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS rules(
            id TEXT PRIMARY KEY, name TEXT, symbol TEXT, timeframe TEXT,
            condition TEXT, channels TEXT, recipients TEXT,
            enabled INTEGER DEFAULT 1, cooldown_sec INTEGER DEFAULT 0, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS rule_state(
            rule_id TEXT PRIMARY KEY, fired INTEGER DEFAULT 0,
            last_value REAL, last_fired_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT, name TEXT,
            symbol TEXT, timeframe TEXT, description TEXT, value REAL, price REAL, ts TEXT, ts_ms INTEGER)""")
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        # migrate older alert tables that predate ts_ms (epoch ms for the "triggered X ago" counter)
        if "ts_ms" not in [r["name"] for r in c.execute("PRAGMA table_info(alerts)")]:
            c.execute("ALTER TABLE alerts ADD COLUMN ts_ms INTEGER")


def get_setting(key, default=None):
    with _db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key, value):
    with _db(write=True) as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _row_to_rule(r):
    return {
        "id": r["id"], "name": r["name"], "symbol": r["symbol"], "timeframe": r["timeframe"],
        "condition": json.loads(r["condition"]), "channels": json.loads(r["channels"]),
        "recipients": json.loads(r["recipients"]), "enabled": bool(r["enabled"]),
        "cooldown_sec": r["cooldown_sec"], "created_at": r["created_at"],
    }


def list_rules(enabled_only=False):
    q = "SELECT * FROM rules"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY created_at"
    with _db() as c:
        return [_row_to_rule(r) for r in c.execute(q)]


def get_rule(rid):
    with _db() as c:
        r = c.execute("SELECT * FROM rules WHERE id=?", (rid,)).fetchone()
        return _row_to_rule(r) if r else None


def create_rule(rule):
    with _db(write=True) as c:
        c.execute(
            """INSERT INTO rules(id,name,symbol,timeframe,condition,channels,recipients,enabled,cooldown_sec,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (rule["id"], rule["name"], rule["symbol"], rule["timeframe"],
             json.dumps(rule["condition"]), json.dumps(rule.get("channels", [])),
             json.dumps(rule.get("recipients", [])), int(rule.get("enabled", True)),
             rule.get("cooldown_sec", 0), datetime.now(ET).isoformat()))
    return get_rule(rule["id"])


def update_rule(rid, rule):
    with _db(write=True) as c:
        c.execute(
            """UPDATE rules SET name=?,symbol=?,timeframe=?,condition=?,channels=?,recipients=?,enabled=?,cooldown_sec=?
               WHERE id=?""",
            (rule["name"], rule["symbol"], rule["timeframe"], json.dumps(rule["condition"]),
             json.dumps(rule.get("channels", [])), json.dumps(rule.get("recipients", [])),
             int(rule.get("enabled", True)), rule.get("cooldown_sec", 0), rid))
    return get_rule(rid)


def delete_rule(rid):
    with _db(write=True) as c:
        c.execute("DELETE FROM rules WHERE id=?", (rid,))
        c.execute("DELETE FROM rule_state WHERE rule_id=?", (rid,))


def record_transition(rule_id, fired, value):
    """Persist fired-state; return True only on a fresh False->True edge (a new alert)."""
    now = datetime.now(ET).isoformat()
    with _db(write=True) as c:
        row = c.execute("SELECT fired FROM rule_state WHERE rule_id=?", (rule_id,)).fetchone()
        prev = bool(row["fired"]) if row else False
        fresh = fired and not prev
        if row is None:
            c.execute("INSERT INTO rule_state(rule_id,fired,last_value,last_fired_at) VALUES(?,?,?,?)",
                      (rule_id, int(fired), value, now if fresh else None))
        elif fresh:
            c.execute("UPDATE rule_state SET fired=1,last_value=?,last_fired_at=? WHERE rule_id=?",
                      (value, now, rule_id))
        else:
            c.execute("UPDATE rule_state SET fired=?,last_value=? WHERE rule_id=?",
                      (int(fired), value, rule_id))
        return fresh


def append_alert(rule, description, value, price):
    with _db(write=True) as c:
        c.execute(
            "INSERT INTO alerts(rule_id,name,symbol,timeframe,description,value,price,ts,ts_ms) VALUES(?,?,?,?,?,?,?,?,?)",
            (rule["id"], rule["name"], rule["symbol"], rule["timeframe"], description, value, price,
             datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"), int(time.time() * 1000)))


def list_alerts(limit=50):
    with _db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))]
