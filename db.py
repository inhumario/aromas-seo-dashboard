"""Capa de persistencia con Postgres."""
import os
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row


DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL no configurado")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_metrics (
    id BIGSERIAL PRIMARY KEY,
    metric_date DATE NOT NULL,
    source TEXT NOT NULL,
    dimensions JSONB NOT NULL DEFAULT '{}'::jsonb,
    metrics JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_daily_metrics_date_source ON daily_metrics (metric_date, source);
CREATE INDEX IF NOT EXISTS idx_daily_metrics_dims ON daily_metrics USING GIN (dimensions);

CREATE TABLE IF NOT EXISTS merchant_snapshots (
    id BIGSERIAL PRIMARY KEY,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    legitimate_total INT,
    all_total INT,
    legit_disapproved INT,
    legit_warnings INT,
    legit_clean INT,
    sources_breakdown JSONB,
    issues_by_code JSONB
);
CREATE INDEX IF NOT EXISTS idx_merchant_snapshots_captured ON merchant_snapshots (captured_at DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    happened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    details JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_events_happened ON audit_events (happened_at DESC);

CREATE TABLE IF NOT EXISTS alert_rules (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    metric_type TEXT NOT NULL,
    condition TEXT NOT NULL,
    threshold NUMERIC NOT NULL,
    compare_window TEXT,
    notify_emails TEXT[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_evaluated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS alert_events (
    id BIGSERIAL PRIMARY KEY,
    rule_id BIGINT REFERENCES alert_rules(id) ON DELETE CASCADE,
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_value NUMERIC,
    reference_value NUMERIC,
    explanation TEXT,
    sent_to TEXT[]
);
CREATE INDEX IF NOT EXISTS idx_alert_events_rule ON alert_events (rule_id, triggered_at DESC);
"""


def init_db():
    with get_conn() as c:
        c.execute(SCHEMA)
        c.execute("INSERT INTO schema_version (version) VALUES (4) ON CONFLICT DO NOTHING")


# ---------- Snapshots Merchant ----------

def insert_merchant_snapshot(stats: dict):
    with get_conn() as c:
        c.execute(
            """
            INSERT INTO merchant_snapshots
            (legitimate_total, all_total, legit_disapproved, legit_warnings, legit_clean, sources_breakdown, issues_by_code)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            """,
            (
                stats.get("legitimate_total"),
                stats.get("all_total"),
                stats.get("legit_disapproved"),
                stats.get("legit_warnings"),
                stats.get("legit_clean"),
                json.dumps(stats.get("sources_breakdown", {})),
                json.dumps(stats.get("issues_by_code", {})),
            ),
        )


def get_merchant_history(days: int = 90):
    with get_conn() as c:
        r = c.execute(
            """
            SELECT captured_at, legitimate_total, legit_disapproved, legit_warnings, legit_clean, all_total
            FROM merchant_snapshots
            WHERE captured_at >= NOW() - INTERVAL '%s days'
            ORDER BY captured_at ASC
            """,
            (days,),
        )
        return list(r)


def latest_merchant_snapshot():
    with get_conn() as c:
        r = c.execute("SELECT * FROM merchant_snapshots ORDER BY captured_at DESC LIMIT 1")
        return r.fetchone()


# ---------- Métricas diarias ----------

def upsert_daily_rows(source: str, rows: list[dict]):
    if not rows:
        return 0
    with get_conn() as c:
        dates = {r['metric_date'] for r in rows}
        if dates:
            c.execute(
                "DELETE FROM daily_metrics WHERE source=%s AND metric_date = ANY(%s)",
                (source, list(dates)),
            )
        for r in rows:
            c.execute(
                """
                INSERT INTO daily_metrics (metric_date, source, dimensions, metrics)
                VALUES (%s, %s, %s::jsonb, %s::jsonb)
                """,
                (r['metric_date'], source, json.dumps(r['dimensions']), json.dumps(r['metrics'])),
            )
    return len(rows)


def get_daily_series(source: str, days: int = 90):
    with get_conn() as c:
        r = c.execute(
            """
            SELECT metric_date, dimensions, metrics
            FROM daily_metrics
            WHERE source=%s AND metric_date >= CURRENT_DATE - INTERVAL '%s days'
            ORDER BY metric_date ASC
            """,
            (source, days),
        )
        return list(r)


# ---------- Audit events ----------

def add_audit_event(category: str, title: str, details: dict | None = None):
    with get_conn() as c:
        c.execute(
            "INSERT INTO audit_events (category, title, details) VALUES (%s, %s, %s::jsonb)",
            (category, title, json.dumps(details or {})),
        )


def get_audit_events(days: int = 365):
    with get_conn() as c:
        r = c.execute(
            """
            SELECT happened_at, category, title, details
            FROM audit_events
            WHERE happened_at >= NOW() - INTERVAL '%s days'
            ORDER BY happened_at DESC
            """,
            (days,),
        )
        return list(r)


def last_merchant_snapshot_age_hours() -> float | None:
    with get_conn() as c:
        r = c.execute("SELECT captured_at FROM merchant_snapshots ORDER BY captured_at DESC LIMIT 1")
        row = r.fetchone()
        if not row:
            return None
        delta = datetime.now(timezone.utc) - row['captured_at']
        return delta.total_seconds() / 3600


# ---------- Alert rules ----------

def create_alert_rule(name: str, metric_type: str, condition: str, threshold: float,
                     compare_window: str | None, notify_emails: list[str]) -> int:
    with get_conn() as c:
        r = c.execute(
            """
            INSERT INTO alert_rules (name, metric_type, condition, threshold, compare_window, notify_emails)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (name, metric_type, condition, threshold, compare_window, notify_emails),
        )
        return r.fetchone()['id']


def list_alert_rules():
    with get_conn() as c:
        r = c.execute("SELECT * FROM alert_rules ORDER BY created_at DESC")
        return list(r)


def toggle_alert_rule(rule_id: int, enabled: bool):
    with get_conn() as c:
        c.execute("UPDATE alert_rules SET enabled=%s WHERE id=%s", (enabled, rule_id))


def delete_alert_rule(rule_id: int):
    with get_conn() as c:
        c.execute("DELETE FROM alert_rules WHERE id=%s", (rule_id,))


def list_alert_events(rule_id: int | None = None, limit: int = 50):
    with get_conn() as c:
        if rule_id:
            r = c.execute(
                """SELECT ae.*, ar.name AS rule_name
                   FROM alert_events ae LEFT JOIN alert_rules ar ON ae.rule_id=ar.id
                   WHERE ae.rule_id=%s ORDER BY triggered_at DESC LIMIT %s""",
                (rule_id, limit),
            )
        else:
            r = c.execute(
                """SELECT ae.*, ar.name AS rule_name
                   FROM alert_events ae LEFT JOIN alert_rules ar ON ae.rule_id=ar.id
                   ORDER BY triggered_at DESC LIMIT %s""",
                (limit,),
            )
        return list(r)


def record_alert_event(rule_id: int, metric_value: float, reference_value: float,
                       explanation: str, sent_to: list[str]):
    with get_conn() as c:
        c.execute(
            """INSERT INTO alert_events (rule_id, metric_value, reference_value, explanation, sent_to)
               VALUES (%s, %s, %s, %s, %s)""",
            (rule_id, metric_value, reference_value, explanation, sent_to),
        )
        c.execute("UPDATE alert_rules SET last_evaluated_at=NOW() WHERE id=%s", (rule_id,))


def mark_evaluated(rule_id: int):
    with get_conn() as c:
        c.execute("UPDATE alert_rules SET last_evaluated_at=NOW() WHERE id=%s", (rule_id,))
