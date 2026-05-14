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

-- Métricas diarias por fuente. Una fila por (date, source, dimension_key).
-- dimensions JSONB permite serializar dimensiones distintas (host, query, page, etc.).
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

-- Estado del Merchant Center capturado por día (no es diario natural, es snapshot).
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

-- Eventos de auditoría / hitos manuales (limpiezas, cambios de theme, etc.)
CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    happened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    details JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_events_happened ON audit_events (happened_at DESC);
"""


def init_db():
    with get_conn() as c:
        c.execute(SCHEMA)
        c.execute("INSERT INTO schema_version (version) VALUES (1) ON CONFLICT DO NOTHING")


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


# ---------- Métricas diarias ----------

def upsert_daily_rows(source: str, rows: list[dict]):
    """rows: lista de {'metric_date': date, 'dimensions': dict, 'metrics': dict}."""
    if not rows:
        return 0
    with get_conn() as c:
        # Estrategia simple: borrar las del rango fechas + source y reinsertar.
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
