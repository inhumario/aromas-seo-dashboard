"""Evaluador de alertas y envío de emails."""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date, datetime, timedelta

import db


# ---------- Tipos de métrica soportados ----------

METRIC_TYPES = {
    "gsc_com.clicks": "GSC clicks orgánicos (.com)",
    "gsc_com.impressions": "GSC impresiones (.com)",
    "gsc_com.position": "GSC posición media (.com)",
    "gsc_com.ctr": "GSC CTR (.com)",
    "gsc_eu.clicks": "GSC clicks orgánicos (.eu)",
    "gsc_eu.impressions": "GSC impresiones (.eu)",
    "ga4.sessions": "GA4 sesiones (todos los hostnames)",
    "ga4.revenue": "GA4 revenue diario",
    "ga4.transactions": "GA4 pedidos diarios",
    "merchant.disapproved": "Merchant productos rechazados (legítimos)",
    "merchant.warnings": "Merchant productos con warnings (legítimos)",
}

CONDITIONS = {
    "lt": "es menor que",
    "gt": "es mayor que",
    "pct_drop_vs_avg": "cae más de X% vs media histórica",
    "pct_rise_vs_avg": "sube más de X% vs media histórica",
}

COMPARE_WINDOWS = {
    "last_7d_avg": "media de los últimos 7 días",
    "last_30d_avg": "media de los últimos 30 días",
}


def _latest_daily_value(source: str, metric_key: str, days_back: int = 1) -> tuple[float | None, date | None]:
    """Devuelve el valor más reciente disponible de daily_metrics[source][metric_key]."""
    series = db.get_daily_series(source, days=14)
    if not series:
        return None, None
    if "." in metric_key:
        metric_key = metric_key.split(".", 1)[1]
    # Agregar por fecha sumando dimensions (ej. múltiples hosts)
    by_date: dict[date, float] = {}
    for r in series:
        d = r["metric_date"]
        val = (r["metrics"] or {}).get(metric_key)
        if val is None:
            continue
        by_date[d] = by_date.get(d, 0) + float(val)
    if not by_date:
        return None, None
    # Si la métrica es 'position' o 'ctr', no sumar sino media — pero por simplicidad coger la última fecha
    sorted_dates = sorted(by_date.keys(), reverse=True)
    last = sorted_dates[0]
    return by_date[last], last


def _avg_last_n_days(source: str, metric_key: str, n: int, exclude_latest: bool = True) -> float | None:
    series = db.get_daily_series(source, days=n + 3)
    if not series:
        return None
    if "." in metric_key:
        metric_key = metric_key.split(".", 1)[1]
    by_date: dict[date, float] = {}
    for r in series:
        d = r["metric_date"]
        val = (r["metrics"] or {}).get(metric_key)
        if val is None:
            continue
        by_date[d] = by_date.get(d, 0) + float(val)
    if not by_date:
        return None
    sorted_dates = sorted(by_date.keys(), reverse=True)
    if exclude_latest and len(sorted_dates) > 1:
        sorted_dates = sorted_dates[1:]
    take = sorted_dates[:n]
    if not take:
        return None
    return sum(by_date[d] for d in take) / len(take)


def _merchant_current(metric: str) -> float | None:
    snap = db.latest_merchant_snapshot()
    if not snap:
        return None
    mapping = {"disapproved": "legit_disapproved", "warnings": "legit_warnings"}
    key = mapping.get(metric, metric)
    return float(snap.get(key) or 0)


def evaluate_rule(rule: dict) -> dict:
    """Evalúa una regla y devuelve dict con campos: triggered, metric_value, reference_value, explanation."""
    metric_type = rule["metric_type"]
    cond = rule["condition"]
    threshold = float(rule["threshold"])
    window = rule.get("compare_window")

    # Obtener valor actual + referencia
    if metric_type.startswith("merchant."):
        metric_value = _merchant_current(metric_type.split(".", 1)[1])
        reference_value = None
    elif metric_type.startswith("gsc_com.") or metric_type.startswith("gsc_eu.") or metric_type.startswith("ga4."):
        source = metric_type.split(".")[0]
        if source == "ga4":
            source = "ga4_hostname"
        metric_value, _date = _latest_daily_value(source, metric_type)
        reference_value = None
        if cond in ("pct_drop_vs_avg", "pct_rise_vs_avg"):
            n = 7 if window == "last_7d_avg" else 30
            reference_value = _avg_last_n_days(source, metric_type, n)
    else:
        return {"triggered": False, "metric_value": None, "reference_value": None,
                "explanation": f"metric_type desconocido: {metric_type}"}

    if metric_value is None:
        return {"triggered": False, "metric_value": None, "reference_value": reference_value,
                "explanation": "sin datos disponibles"}

    triggered = False
    explanation = ""
    if cond == "lt":
        triggered = metric_value < threshold
        explanation = f"{metric_type} = {metric_value:.2f} {'<' if triggered else '>='} {threshold}"
    elif cond == "gt":
        triggered = metric_value > threshold
        explanation = f"{metric_type} = {metric_value:.2f} {'>' if triggered else '<='} {threshold}"
    elif cond == "pct_drop_vs_avg":
        if reference_value and reference_value > 0:
            pct_change = (metric_value - reference_value) / reference_value * 100
            triggered = pct_change <= -abs(threshold)
            explanation = (f"{metric_type} = {metric_value:.2f} vs media {reference_value:.2f} "
                          f"({pct_change:+.1f}%) — umbral -{abs(threshold)}%")
    elif cond == "pct_rise_vs_avg":
        if reference_value and reference_value > 0:
            pct_change = (metric_value - reference_value) / reference_value * 100
            triggered = pct_change >= abs(threshold)
            explanation = (f"{metric_type} = {metric_value:.2f} vs media {reference_value:.2f} "
                          f"({pct_change:+.1f}%) — umbral +{abs(threshold)}%")

    return {
        "triggered": triggered,
        "metric_value": metric_value,
        "reference_value": reference_value,
        "explanation": explanation,
    }


# ---------- Email via Gmail SMTP ----------

def send_alert_email(to_addrs: list[str], rule_name: str, explanation: str):
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        return False, "GMAIL_USER/PASSWORD no configurados en ENV"

    subject = f"[Aromas SEO] Alerta: {rule_name}"
    body_html = f"""<html><body>
    <h3 style="color:#c00">⚠️ Alerta SEO disparada</h3>
    <p><strong>{rule_name}</strong></p>
    <p>{explanation}</p>
    <p>Ver detalles en <a href="https://seo.aromasdete.com">seo.aromasdete.com</a></p>
    <hr>
    <p style="font-size:11px;color:#666">Esta alerta se ha disparado tras el snapshot diario del dashboard SEO.
    Para ajustar el umbral o desactivar la regla, entra en la tab "🔔 Alertas" del dashboard.</p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(body_html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, password)
            s.sendmail(user, to_addrs, msg.as_string())
        return True, "ok"
    except Exception as e:
        return False, str(e)


def evaluate_all_and_notify() -> list[dict]:
    """Evalúa todas las reglas activas, dispara emails y registra eventos.
    Devuelve lista de resultados.
    """
    results = []
    for rule in db.list_alert_rules():
        if not rule["enabled"]:
            continue
        ev = evaluate_rule(rule)
        rule_result = {"rule_id": rule["id"], "rule_name": rule["name"], **ev}
        if ev["triggered"]:
            emails = rule["notify_emails"] or []
            sent_ok = []
            for em in emails:
                ok, msg = send_alert_email([em], rule["name"], ev["explanation"])
                if ok:
                    sent_ok.append(em)
            db.record_alert_event(
                rule["id"],
                ev["metric_value"] or 0,
                ev["reference_value"] or 0,
                ev["explanation"],
                sent_ok,
            )
            rule_result["sent_to"] = sent_ok
        else:
            db.mark_evaluated(rule["id"])
        results.append(rule_result)
    return results
