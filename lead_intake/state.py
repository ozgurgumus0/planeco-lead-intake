import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .features import from_intake
from .files import json_text, parse_time
from .routing import LISBON

CHANNELS = ("google", "meta", "organic", "partner", "bing", "unknown", "other")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    external_id TEXT PRIMARY KEY,
    record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_patches (
    event_id TEXT PRIMARY KEY,
    external_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    fields TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_by_lead ON pending_patches (external_id, occurred_at, event_id);
CREATE TABLE IF NOT EXISTS tombstones (
    external_id TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exports (
    batch_id TEXT PRIMARY KEY,
    input_fingerprint TEXT NOT NULL,
    destination TEXT NOT NULL,
    summary TEXT NOT NULL,
    manifest TEXT NOT NULL,
    documents TEXT,
    published_at TEXT,
    crm_confirmed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS single_pending_export ON exports ((1)) WHERE published_at IS NULL;
CREATE TABLE IF NOT EXISTS crm_actions (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES exports (batch_id),
    external_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('update', 'delete')),
    payload TEXT,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS crm_actions_by_lead ON crm_actions (external_id, operation);
CREATE TABLE IF NOT EXISTS arrival_counts (
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    channel TEXT NOT NULL,
    total INTEGER NOT NULL,
    PRIMARY KEY (day, source, channel)
);
CREATE TABLE IF NOT EXISTS export_labels (
    batch_id TEXT PRIMARY KEY REFERENCES exports (batch_id),
    filename TEXT NOT NULL
);
"""


@contextmanager
def open_state(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA secure_delete = ON")
        conn.executescript(SCHEMA)
        marker = "arrival_counts_initialized"
        if conn.execute("SELECT 1 FROM settings WHERE name = ?", (marker,)).fetchone() is None:
            with transaction(conn):
                if conn.execute("INSERT OR IGNORE INTO settings VALUES (?, 'yes')", (marker,)).rowcount:
                    for row in conn.execute("SELECT record FROM leads"):
                        lead = json.loads(row["record"])
                        record_arrival(conn, parse_time(lead["created_at_utc"]), lead["fields"])
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def record_arrival(conn, when, fields: dict) -> None:
    """Keep only a daily aggregate. It survives erasure of the contact record."""
    day = when.astimezone(LISBON).date().isoformat()
    channel = from_intake(fields)["channel"]
    if channel not in CHANNELS:
        channel = "other"
    conn.execute("""
        INSERT INTO arrival_counts VALUES (?, ?, ?, 1)
        ON CONFLICT (day, source, channel) DO UPDATE SET total = total + 1
    """, (day, fields["source"], channel))


def crm_actions(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT a.action_id, a.batch_id, a.external_id, a.operation, a.payload
        FROM crm_actions AS a JOIN exports AS e USING (batch_id)
        WHERE e.published_at IS NOT NULL AND a.confirmed_at IS NULL
        ORDER BY a.action_id
    """)
    return [
        {**dict(row), "payload": json.loads(row["payload"]) if row["payload"] is not None else None}
        for row in rows
    ]


def record_crm_action(conn, batch_id: str, external_id: str, row: dict | None) -> None:
    if row is None:
        conn.execute("DELETE FROM crm_actions WHERE external_id = ? AND operation = 'update'", (external_id,))
    conn.execute(
        "INSERT INTO crm_actions (batch_id, external_id, operation, payload) VALUES (?, ?, ?, ?)",
        (batch_id, external_id, "delete" if row is None else "update", json_text(row) if row is not None else None),
    )


def confirm_action(conn, action_id: int) -> None:
    with transaction(conn):
        cursor = conn.execute("""
            UPDATE crm_actions SET confirmed_at = coalesce(confirmed_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            payload = NULL
            WHERE action_id = ? AND batch_id IN (SELECT batch_id FROM exports WHERE published_at IS NOT NULL)
        """, (action_id,))
        if cursor.rowcount != 1:
            raise ValueError(f"no published CRM action with ID {action_id}")


def confirm_export(conn, batch_id: str) -> None:
    with transaction(conn):
        cursor = conn.execute("""
            UPDATE exports SET crm_confirmed_at = coalesce(crm_confirmed_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            WHERE batch_id = ? AND published_at IS NOT NULL
        """, (batch_id,))
        if cursor.rowcount != 1:
            raise ValueError(f"no published export with ID {batch_id}")
