"""
storage.py — SQLite-backed audit log + submission state for Provenance Guard.

Two tables:
  submissions: current state of each piece of content (one row per content_id)
  audit_log:   append-only structured log of every decision + appeal event
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("PROVENANCE_DB_PATH", Path(__file__).parent / "provenance_guard.db"))


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            content_id TEXT PRIMARY KEY,
            creator_id TEXT NOT NULL,
            text TEXT NOT NULL,
            llm_score REAL,
            llm_rationale TEXT,
            stylo_score REAL,
            stylo_metrics TEXT,
            confidence REAL,
            attribution TEXT,
            label TEXT,
            status TEXT NOT NULL DEFAULT 'classified',
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id TEXT NOT NULL,
            creator_id TEXT,
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            attribution TEXT,
            confidence REAL,
            llm_score REAL,
            stylo_score REAL,
            label TEXT,
            status TEXT,
            appeal_reasoning TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def create_submission(
    content_id,
    creator_id,
    text,
    llm_score,
    llm_rationale,
    stylo_score,
    stylo_metrics,
    confidence,
    attribution,
    label,
):
    created_at = _now()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO submissions
            (content_id, creator_id, text, llm_score, llm_rationale,
             stylo_score, stylo_metrics, confidence, attribution, label,
             status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'classified', ?)
        """,
        (
            content_id,
            creator_id,
            text,
            llm_score,
            llm_rationale,
            stylo_score,
            json.dumps(stylo_metrics),
            confidence,
            attribution,
            label,
            created_at,
        ),
    )
    conn.commit()
    conn.close()

    log_event(
        content_id=content_id,
        creator_id=creator_id,
        event_type="submission",
        attribution=attribution,
        confidence=confidence,
        llm_score=llm_score,
        stylo_score=stylo_score,
        label=label,
        status="classified",
        timestamp=created_at,
    )
    return created_at


def get_submission(content_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM submissions WHERE content_id = ?", (content_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def update_submission_status(content_id, status):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE submissions SET status = ? WHERE content_id = ?",
        (status, content_id),
    )
    conn.commit()
    updated = cur.rowcount
    conn.close()
    return updated > 0


def log_event(
    content_id,
    event_type,
    creator_id=None,
    attribution=None,
    confidence=None,
    llm_score=None,
    stylo_score=None,
    label=None,
    status=None,
    appeal_reasoning=None,
    timestamp=None,
):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO audit_log
            (content_id, creator_id, event_type, timestamp, attribution,
             confidence, llm_score, stylo_score, label, status, appeal_reasoning)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            content_id,
            creator_id,
            event_type,
            timestamp or _now(),
            attribution,
            confidence,
            llm_score,
            stylo_score,
            label,
            status,
            appeal_reasoning,
        ),
    )
    conn.commit()
    conn.close()


def get_recent_log(limit=20):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
