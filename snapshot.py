"""Script standalone que captura snapshot diario y evalúa alertas.

Uso:
    python snapshot.py        # captura + evalúa alertas
    python snapshot.py --skip-alerts   # solo snapshot
"""
import os
import sys
import json
import base64
from datetime import date, datetime, timedelta

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

import db
import alerts


GA4_PROPERTY = "properties/316499868"
MERCHANT_ID = "115390048"


def get_clients():
    b64 = os.environ.get("GOOGLE_TOKEN_B64", "")
    if not b64:
        raise SystemExit("Falta GOOGLE_TOKEN_B64")
    data = json.loads(base64.b64decode(b64))
    tmp = "/tmp/google_token_snapshot.json"
    with open(tmp, "w") as f:
        json.dump(data, f)
    creds = Credentials.from_authorized_user_file(tmp)
    return {
        "gsc": build("searchconsole", "v1", credentials=creds, cache_discovery=False),
        "ga_data": build("analyticsdata", "v1beta", credentials=creds, cache_discovery=False),
        "mc": build("content", "v2.1", credentials=creds, cache_discovery=False),
    }


def capture_merchant(clients):
    legitimate_ids = set()
    other_sources = {}
    req = clients["mc"].products().list(merchantId=MERCHANT_ID, maxResults=250)
    while req:
        r = req.execute()
        for p in r.get("resources", []):
            src = p.get("source"); lang = p.get("contentLanguage"); country = p.get("targetCountry")
            key = f"{src}|{lang}|{country}"
            if src == "api" and lang == "es" and country == "ES":
                legitimate_ids.add(p.get("id"))
            other_sources[key] = other_sources.get(key, 0) + 1
        req = clients["mc"].products().list_next(req, r)
    stats = {
        "legitimate_total": len(legitimate_ids),
        "all_total": sum(other_sources.values()),
        "sources_breakdown": other_sources,
        "legit_disapproved": 0, "legit_warnings": 0, "legit_clean": 0,
        "issues_by_code": {},
    }
    req = clients["mc"].productstatuses().list(merchantId=MERCHANT_ID, maxResults=250)
    while req:
        r = req.execute()
        for p in r.get("resources", []):
            pid = p.get("productId")
            if pid not in legitimate_ids:
                continue
            issues = p.get("itemLevelIssues") or []
            is_disapp = any(d.get("status") == "disapproved" for d in (p.get("destinationStatuses") or []))
            if is_disapp:
                stats["legit_disapproved"] += 1
            elif issues:
                stats["legit_warnings"] += 1
            else:
                stats["legit_clean"] += 1
            for iss in issues:
                code = iss.get("code", "?")
                stats["issues_by_code"][code] = stats["issues_by_code"].get(code, 0) + 1
        req = clients["mc"].productstatuses().list_next(req, r)
    db.insert_merchant_snapshot(stats)
    return stats


def capture_ga4(clients, days=90):
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    res = clients["ga_data"].properties().runReport(property=GA4_PROPERTY, body={
        "dateRanges": [{"startDate": str(start), "endDate": str(end)}],
        "dimensions": [{"name": "date"}, {"name": "hostName"}],
        "metrics": [{"name": m} for m in ["sessions", "totalUsers", "screenPageViews", "purchaseRevenue", "transactions"]],
        "limit": 100000,
    }).execute()
    rows = []
    for r in res.get("rows", []):
        try:
            d = datetime.strptime(r["dimensionValues"][0]["value"], "%Y%m%d").date()
        except ValueError:
            continue
        mv = r["metricValues"]
        rows.append({
            "metric_date": d,
            "dimensions": {"hostName": r["dimensionValues"][1]["value"]},
            "metrics": {
                "sessions": float(mv[0]["value"]),
                "totalUsers": float(mv[1]["value"]),
                "pageViews": float(mv[2]["value"]),
                "revenue": float(mv[3]["value"]),
                "transactions": float(mv[4]["value"]),
            },
        })
    return db.upsert_daily_rows("ga4_hostname", rows)


def capture_gsc(clients, days=90):
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    total = 0
    for site, src in [("sc-domain:aromasdete.com", "gsc_com"), ("sc-domain:aromasdete.eu", "gsc_eu")]:
        try:
            res = clients["gsc"].searchanalytics().query(siteUrl=site, body={
                "startDate": str(start), "endDate": str(end),
                "dimensions": ["date"], "rowLimit": 1000,
            }).execute()
            rows = []
            for r in res.get("rows", []):
                try:
                    d = date.fromisoformat(r["keys"][0])
                except ValueError:
                    continue
                rows.append({
                    "metric_date": d, "dimensions": {},
                    "metrics": {
                        "clicks": r["clicks"],
                        "impressions": r["impressions"],
                        "ctr": r["ctr"],
                        "position": r["position"],
                    },
                })
            total += db.upsert_daily_rows(src, rows)
        except Exception as e:
            print(f"  warn: gsc {site}: {e}", flush=True)
    return total


def main():
    skip_alerts = "--skip-alerts" in sys.argv
    print(f"[{datetime.now().isoformat()}] Iniciando snapshot...", flush=True)
    db.init_db()
    clients = get_clients()
    merchant = capture_merchant(clients)
    print(f"  Merchant: {merchant['legitimate_total']} legítimos, {merchant['legit_disapproved']} rechazados", flush=True)
    ga4_rows = capture_ga4(clients)
    print(f"  GA4: {ga4_rows} filas diarias", flush=True)
    gsc_rows = capture_gsc(clients)
    print(f"  GSC: {gsc_rows} filas diarias", flush=True)

    if not skip_alerts:
        print(f"[{datetime.now().isoformat()}] Evaluando alertas...", flush=True)
        results = alerts.evaluate_all_and_notify()
        for r in results:
            mark = "🔔 DISPARADA" if r.get("triggered") else "·"
            print(f"  {mark} #{r['rule_id']} {r['rule_name']}: {r.get('explanation','')}", flush=True)
            if r.get("triggered") and r.get("sent_to"):
                print(f"      email enviado a: {r['sent_to']}", flush=True)

    print(f"[{datetime.now().isoformat()}] Snapshot completado.", flush=True)


if __name__ == "__main__":
    main()
