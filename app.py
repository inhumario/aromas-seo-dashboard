import os
import json
import base64
from datetime import date, datetime, timedelta, timezone

import streamlit as st
import pandas as pd
import plotly.express as px
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

from version import __version__, RELEASE_DATE
import db
import alerts as alerts_mod

# ---------- Config Streamlit ----------
st.set_page_config(
    page_title=f"SEO Aromas v{__version__}",
    page_icon="🍵",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    '<meta name="robots" content="noindex, nofollow, noarchive, nosnippet">'
    '<meta name="googlebot" content="noindex, nofollow">',
    unsafe_allow_html=True,
)

# ---------- Auth ----------
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

def check_password():
    if "auth_ok" not in st.session_state:
        st.session_state.auth_ok = False
    if st.session_state.auth_ok:
        return True
    st.title("🍵 SEO Aromas — acceso")
    pwd = st.text_input("Contraseña", type="password")
    if st.button("Entrar"):
        if DASHBOARD_PASSWORD and pwd == DASHBOARD_PASSWORD:
            st.session_state.auth_ok = True
            st.rerun()
        else:
            st.error("Contraseña incorrecta")
    return False

if not check_password():
    st.stop()

# ---------- Credenciales Google ----------
@st.cache_resource
def get_creds():
    b64 = os.environ.get("GOOGLE_TOKEN_B64", "")
    if not b64:
        st.error("Falta GOOGLE_TOKEN_B64 en variables de entorno")
        st.stop()
    tmp = "/tmp/google_token.json"
    with open(tmp, "w") as f:
        json.dump(json.loads(base64.b64decode(b64)), f)
    return Credentials.from_authorized_user_file(tmp)

@st.cache_resource
def get_clients():
    creds = get_creds()
    return {
        "gsc": build("searchconsole", "v1", credentials=creds, cache_discovery=False),
        "ga_data": build("analyticsdata", "v1beta", credentials=creds, cache_discovery=False),
        "ga_admin": build("analyticsadmin", "v1beta", credentials=creds, cache_discovery=False),
        "mc": build("content", "v2.1", credentials=creds, cache_discovery=False),
    }

clients = get_clients()
GA4_PROPERTY = "properties/316499868"
MERCHANT_ID = "115390048"
CACHE_TTL = 60 * 15

# ---------- DB init ----------
@st.cache_resource
def init_database():
    try:
        db.init_db()
        return True
    except Exception as e:
        st.warning(f"Postgres no inicializado: {e}")
        return False

db_ok = init_database()

# ---------- Helpers ----------
def now_es_str():
    return datetime.now(timezone(timedelta(hours=2))).strftime("%Y-%m-%d %H:%M:%S")


# ---------- Wrappers de fetch (con caché + escritura a BD) ----------

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def gsc_query(site_url, start, end, dimensions, row_limit=1000):
    res = clients["gsc"].searchanalytics().query(siteUrl=site_url, body={
        "startDate": start, "endDate": end,
        "dimensions": dimensions, "rowLimit": row_limit,
    }).execute()
    rows = res.get("rows", [])
    if not rows:
        return pd.DataFrame(), now_es_str()
    df = pd.DataFrame([{
        **{d: r["keys"][i] for i, d in enumerate(dimensions)},
        "clicks": r["clicks"],
        "impressions": r["impressions"],
        "ctr": r["ctr"],
        "position": r["position"],
    } for r in rows])
    return df, now_es_str()

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def ga4_report(start, end, dimensions, metrics, order_by_metric=None, limit=1000):
    body = {
        "dateRanges": [{"startDate": start, "endDate": end}],
        "dimensions": [{"name": d} for d in dimensions],
        "metrics": [{"name": m} for m in metrics],
        "limit": limit,
    }
    if order_by_metric:
        body["orderBys"] = [{"metric": {"metricName": order_by_metric}, "desc": True}]
    res = clients["ga_data"].properties().runReport(property=GA4_PROPERTY, body=body).execute()
    rows = res.get("rows", [])
    if not rows:
        return pd.DataFrame(), now_es_str()
    data = []
    for r in rows:
        d = {dim: r["dimensionValues"][i]["value"] for i, dim in enumerate(dimensions)}
        for i, m in enumerate(metrics):
            v = r["metricValues"][i]["value"]
            try:
                d[m] = float(v)
            except ValueError:
                d[m] = v
        data.append(d)
    return pd.DataFrame(data), now_es_str()

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def merchant_full_status():
    legitimate_ids = set()
    other_sources = {}
    req = clients["mc"].products().list(merchantId=MERCHANT_ID, maxResults=250)
    while req:
        r = req.execute()
        for p in r.get("resources", []):
            src = p.get("source")
            lang = p.get("contentLanguage")
            country = p.get("targetCountry")
            key = f"{src}|{lang}|{country}"
            if src == "api" and lang == "es" and country == "ES":
                legitimate_ids.add(p.get("id"))
            other_sources[key] = other_sources.get(key, 0) + 1
        req = clients["mc"].products().list_next(req, r)
    stats = {
        "legitimate_total": len(legitimate_ids),
        "all_total": sum(other_sources.values()),
        "sources_breakdown": other_sources,
        "legit_disapproved": 0,
        "legit_warnings": 0,
        "legit_clean": 0,
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
    return stats, now_es_str()


# ---------- Snapshot diario al cargar (throttled) ----------

def _maybe_take_snapshots():
    """Si el último snapshot tiene >20h, captura uno nuevo de Merchant + métricas diarias GSC/GA4 últimos N días."""
    if not db_ok:
        return False, "Postgres no disponible"
    try:
        age = db.last_merchant_snapshot_age_hours()
    except Exception as e:
        return False, f"err leyendo Postgres: {e}"

    if age is not None and age < 20:
        return False, f"último snapshot hace {age:.1f}h, no toca todavía"

    # 1) Merchant snapshot
    stats, _ = merchant_full_status()
    db.insert_merchant_snapshot(stats)

    # 2) GA4 daily series (hostname + métricas) últimos 90 días
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=89)
    body = {
        "dateRanges": [{"startDate": str(start), "endDate": str(end)}],
        "dimensions": [{"name": "date"}, {"name": "hostName"}],
        "metrics": [{"name": m} for m in ["sessions", "totalUsers", "screenPageViews", "purchaseRevenue", "transactions"]],
        "limit": 100000,
    }
    res = clients["ga_data"].properties().runReport(property=GA4_PROPERTY, body=body).execute()
    rows = []
    for r in res.get("rows", []):
        date_str = r["dimensionValues"][0]["value"]  # YYYYMMDD
        host = r["dimensionValues"][1]["value"]
        mv = r["metricValues"]
        try:
            d = datetime.strptime(date_str, "%Y%m%d").date()
        except ValueError:
            continue
        rows.append({
            "metric_date": d,
            "dimensions": {"hostName": host},
            "metrics": {
                "sessions": float(mv[0]["value"]),
                "totalUsers": float(mv[1]["value"]),
                "pageViews": float(mv[2]["value"]),
                "revenue": float(mv[3]["value"]),
                "transactions": float(mv[4]["value"]),
            },
        })
    db.upsert_daily_rows("ga4_hostname", rows)

    # 3) GSC daily (.com)
    res = clients["gsc"].searchanalytics().query(siteUrl="sc-domain:aromasdete.com", body={
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
            "metric_date": d,
            "dimensions": {},
            "metrics": {
                "clicks": r["clicks"],
                "impressions": r["impressions"],
                "ctr": r["ctr"],
                "position": r["position"],
            },
        })
    db.upsert_daily_rows("gsc_com", rows)

    # 4) GSC daily (.eu)
    try:
        res = clients["gsc"].searchanalytics().query(siteUrl="sc-domain:aromasdete.eu", body={
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
                "metric_date": d,
                "dimensions": {},
                "metrics": {
                    "clicks": r["clicks"],
                    "impressions": r["impressions"],
                    "ctr": r["ctr"],
                    "position": r["position"],
                },
            })
        db.upsert_daily_rows("gsc_eu", rows)
    except Exception:
        pass

    return True, f"snapshot capturado a las {now_es_str()}"


# ---------- Sidebar ----------
with st.sidebar:
    st.title(f"🍵 SEO Aromas")
    st.caption(f"v{__version__} · {RELEASE_DATE}")
    st.divider()

    st.subheader("Periodo")
    periodo = st.radio("Días", [7, 28, 90, 180], index=2, format_func=lambda d: f"Últimos {d} días", key="periodo_radio")
    end_date = date.today() - timedelta(days=2)
    start_date = end_date - timedelta(days=periodo - 1)
    st.write(f"`{start_date}` → `{end_date}`")

    st.divider()

    if st.button("🔄 Refrescar TODOS los datos", use_container_width=True, type="primary"):
        st.cache_data.clear()
        st.rerun()

    st.caption(f"TTL del caché: {CACHE_TTL // 60} min · Pulsa Refrescar si una métrica no refleja un cambio reciente.")

    st.divider()
    st.subheader("Histórico (Postgres)")
    if db_ok:
        try:
            age = db.last_merchant_snapshot_age_hours()
            if age is None:
                st.warning("Sin snapshots aún")
            else:
                st.success(f"Último snapshot: {age:.1f}h")
        except Exception as e:
            st.warning(f"err: {e}")
        if st.button("📸 Capturar snapshot AHORA", use_container_width=True):
            with st.spinner("Capturando snapshot completo..."):
                try:
                    # Forzar: borrar throttling temporal
                    stats, _ = merchant_full_status()
                    db.insert_merchant_snapshot(stats)
                    # GA4 + GSC
                    end = date.today() - timedelta(days=1)
                    start = end - timedelta(days=89)
                    body = {
                        "dateRanges": [{"startDate": str(start), "endDate": str(end)}],
                        "dimensions": [{"name": "date"}, {"name": "hostName"}],
                        "metrics": [{"name": m} for m in ["sessions", "totalUsers", "screenPageViews", "purchaseRevenue", "transactions"]],
                        "limit": 100000,
                    }
                    res = clients["ga_data"].properties().runReport(property=GA4_PROPERTY, body=body).execute()
                    rows = []
                    for r in res.get("rows", []):
                        try:
                            d = datetime.strptime(r["dimensionValues"][0]["value"], "%Y%m%d").date()
                        except ValueError:
                            continue
                        mv = r["metricValues"]
                        rows.append({"metric_date": d, "dimensions": {"hostName": r["dimensionValues"][1]["value"]},
                                     "metrics": {"sessions": float(mv[0]["value"]), "totalUsers": float(mv[1]["value"]),
                                                 "pageViews": float(mv[2]["value"]), "revenue": float(mv[3]["value"]),
                                                 "transactions": float(mv[4]["value"])}})
                    db.upsert_daily_rows("ga4_hostname", rows)
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
                                rows.append({"metric_date": d, "dimensions": {},
                                             "metrics": {"clicks": r["clicks"], "impressions": r["impressions"],
                                                         "ctr": r["ctr"], "position": r["position"]}})
                            db.upsert_daily_rows(src, rows)
                        except Exception:
                            pass
                    st.success("Snapshot capturado")
                    st.cache_data.clear()
                except Exception as e:
                    st.error(f"err: {e}")
    else:
        st.error("Postgres no conectado")

    # Snapshot automático al cargar (si no se ha hecho en 20h)
    if db_ok:
        try:
            took, msg = _maybe_take_snapshots()
            if took:
                st.caption(f"✓ Auto-snapshot: {msg}")
        except Exception:
            pass

    st.divider()
    st.caption("**Latencia fuentes:**")
    st.caption("· GSC: ~48 h delay (oficial Google)")
    st.caption("· GA4: ~5-15 min reports")
    st.caption("· Merchant: 1-3 h por sync")

    st.divider()
    st.caption(f"[GitHub](https://github.com/inhumario/aromas-seo-dashboard)")

S = str(start_date); E = str(end_date)

# ---------- Tabs ----------
tabs = st.tabs([
    "🍵 Resumen",
    "📈 Histórico",
    "🔍 SEO orgánico",
    "📊 GA4",
    "💰 Google Ads",
    "🛒 Merchant",
    "🔔 Alertas",
    "🎯 Plan de acción",
    "📋 Changelog",
])

# ========== RESUMEN ==========
with tabs[0]:
    col_title, col_refresh = st.columns([5, 1])
    col_title.title("Resumen general")
    if col_refresh.button("🔄 Refrescar", key="r_resumen"):
        gsc_query.clear(); ga4_report.clear()
        st.rerun()
    st.caption(f"Periodo: {S} → {E}")

    col1, col2, col3, col4 = st.columns(4)
    df_hosts, fetched = ga4_report(S, E, ["hostName"],
        ["sessions", "totalUsers", "screenPageViews", "purchaseRevenue", "transactions"])
    if not df_hosts.empty:
        col1.metric("Sesiones", f"{int(df_hosts['sessions'].sum()):,}")
        col2.metric("Revenue", f"{df_hosts['purchaseRevenue'].sum():,.0f} €")
        col3.metric("Pedidos", f"{int(df_hosts['transactions'].sum()):,}")

    df_ads_summary, _ = ga4_report(S, E,
        ["sessionGoogleAdsCampaignName"],
        ["advertiserAdCost", "purchaseRevenue"],
        order_by_metric="advertiserAdCost", limit=50)
    if not df_ads_summary.empty:
        cost = float(df_ads_summary["advertiserAdCost"].sum())
        rev_ads = float(df_ads_summary["purchaseRevenue"].sum())
        if cost > 0:
            col4.metric("ROAS Ads", f"{rev_ads / cost:.2f}x")
    st.caption(f"✨ GA4 a las {fetched}")
    st.divider()

    st.subheader("Tráfico por hostname (GA4)")
    if not df_hosts.empty:
        st.dataframe(df_hosts.sort_values("sessions", ascending=False), use_container_width=True)


# ========== HISTÓRICO ==========
with tabs[1]:
    st.title("📈 Histórico — evolución temporal")
    if not db_ok:
        st.error("Postgres no disponible.")
    else:
        # ---- Merchant evolución ----
        st.subheader("Estado del Merchant Center (todas las capturas)")
        merch_hist = db.get_merchant_history(days=180)
        if not merch_hist:
            st.info("Sin snapshots aún. Pulsa 'Capturar snapshot AHORA' en el sidebar.")
        else:
            df_m = pd.DataFrame(merch_hist)
            df_m["captured_at"] = pd.to_datetime(df_m["captured_at"])
            fig = px.line(df_m, x="captured_at",
                          y=["legitimate_total", "legit_clean", "legit_warnings", "legit_disapproved"],
                          markers=True,
                          title="Productos del feed legítimo (api|es|ES) — evolución")
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(df_m, use_container_width=True)

        st.divider()

        # ---- GSC .com evolución diaria ----
        st.subheader("GSC `aromasdete.com` — métricas diarias")
        gsc_com = db.get_daily_series("gsc_com", days=90)
        if not gsc_com:
            st.info("Sin datos GSC en BD aún.")
        else:
            df_g = pd.DataFrame([{
                "date": row["metric_date"],
                **row["metrics"],
            } for row in gsc_com])
            df_g["date"] = pd.to_datetime(df_g["date"])
            df_g = df_g.sort_values("date")
            c1, c2 = st.columns(2)
            with c1:
                fig = px.line(df_g, x="date", y=["clicks", "impressions"], title="Clicks e Impresiones")
                st.plotly_chart(fig, use_container_width=True)
            with c2:
                fig = px.line(df_g, x="date", y=["ctr", "position"], title="CTR y posición media (eje único)")
                st.plotly_chart(fig, use_container_width=True)

        st.divider()

        # ---- GA4 sesiones por hostname ----
        st.subheader("GA4 sesiones por dominio (diario)")
        ga4_hist = db.get_daily_series("ga4_hostname", days=90)
        if not ga4_hist:
            st.info("Sin datos GA4 en BD aún.")
        else:
            data = []
            for row in ga4_hist:
                data.append({
                    "date": row["metric_date"],
                    "host": (row["dimensions"] or {}).get("hostName", "?"),
                    **row["metrics"],
                })
            df_ga = pd.DataFrame(data)
            df_ga["date"] = pd.to_datetime(df_ga["date"])
            df_ga = df_ga[df_ga["host"].isin([
                "www.aromasdete.com", "www.aromasdete.eu",
                "blog.aromasdete.com", "noticias.aromasdete.com",
            ])]
            c1, c2 = st.columns(2)
            with c1:
                fig = px.line(df_ga.sort_values("date"), x="date", y="sessions", color="host",
                              title="Sesiones diarias por dominio")
                st.plotly_chart(fig, use_container_width=True)
            with c2:
                fig = px.line(df_ga.sort_values("date"), x="date", y="revenue", color="host",
                              title="Revenue diario por dominio")
                st.plotly_chart(fig, use_container_width=True)

        st.divider()

        # ---- Audit events ----
        st.subheader("📌 Hitos / eventos manuales")
        events = db.get_audit_events(days=365)
        if events:
            df_ev = pd.DataFrame(events)
            st.dataframe(df_ev, use_container_width=True)
        else:
            st.info("Sin eventos registrados.")
        with st.expander("Añadir nuevo evento"):
            with st.form("new_event"):
                cat = st.selectbox("Categoría", ["theme_change", "merchant_cleanup", "campaign_launch", "config_change", "other"])
                title = st.text_input("Título")
                details = st.text_area("Detalles (opcional)")
                ok = st.form_submit_button("Guardar")
                if ok and title:
                    db.add_audit_event(cat, title, {"notes": details} if details else None)
                    st.success("Evento guardado")
                    st.rerun()


# ========== SEO ORGÁNICO ==========
with tabs[2]:
    col_title, col_refresh = st.columns([5, 1])
    col_title.title("SEO orgánico — Search Console")
    if col_refresh.button("🔄 Refrescar", key="r_seo"):
        gsc_query.clear(); st.rerun()
    st.caption(f"Periodo: {S} → {E}")
    site = st.selectbox("Propiedad", ["sc-domain:aromasdete.com", "sc-domain:aromasdete.eu"])
    df_q, fetched_q = gsc_query(site, S, E, ["query"], row_limit=2000)
    df_p, _ = gsc_query(site, S, E, ["page"], row_limit=2000)
    if df_q.empty:
        st.info("Sin datos.")
    else:
        st.caption(f"✨ {fetched_q}")
        st.subheader("Oportunidades — pos 5-20 con ≥100 impresiones")
        opp = df_q[(df_q["impressions"] >= 100) & (df_q["position"] >= 5) & (df_q["position"] <= 20)].sort_values("impressions", ascending=False)
        st.caption(f"{len(opp)} queries con potencial")
        st.dataframe(opp.head(50).style.format({"ctr": "{:.2%}", "position": "{:.1f}"}), use_container_width=True)
        st.divider()
        st.subheader("Top 50 queries por clicks")
        st.dataframe(df_q.sort_values("clicks", ascending=False).head(50).style.format({"ctr": "{:.2%}", "position": "{:.1f}"}), use_container_width=True)
        st.divider()
        st.subheader("Top 50 páginas por clicks")
        st.dataframe(df_p.sort_values("clicks", ascending=False).head(50).style.format({"ctr": "{:.2%}", "position": "{:.1f}"}), use_container_width=True)

# ========== GA4 ==========
with tabs[3]:
    col_title, col_refresh = st.columns([5, 1])
    col_title.title("GA4 — analítica")
    if col_refresh.button("🔄 Refrescar", key="r_ga4"):
        ga4_report.clear(); st.rerun()
    st.caption(f"Periodo: {S} → {E}")
    df_pp, fetched_pp = ga4_report(S, E,
        ["hostName", "pagePath"],
        ["sessions", "engagedSessions", "screenPageViews", "purchaseRevenue"],
        order_by_metric="sessions", limit=100)
    if not df_pp.empty:
        st.caption(f"✨ {fetched_pp}")
        st.subheader("Top 30 páginas por sesiones")
        df_pp["url"] = "https://" + df_pp["hostName"] + df_pp["pagePath"]
        st.dataframe(df_pp.head(30), use_container_width=True)
    st.divider()
    st.subheader("Source / Medium")
    df_sm, _ = ga4_report(S, E,
        ["sessionSource", "sessionMedium"],
        ["sessions", "engagedSessions", "conversions", "purchaseRevenue"],
        order_by_metric="sessions", limit=30)
    if not df_sm.empty:
        st.dataframe(df_sm, use_container_width=True)

# ========== ADS ==========
with tabs[4]:
    col_title, col_refresh = st.columns([5, 1])
    col_title.title("Google Ads (datos vía GA4)")
    if col_refresh.button("🔄 Refrescar", key="r_ads"):
        ga4_report.clear(); st.rerun()
    st.caption(f"Periodo: {S} → {E}")
    df_ads, fetched_ads = ga4_report(S, E,
        ["sessionGoogleAdsCampaignName"],
        ["advertiserAdCost", "advertiserAdClicks", "advertiserAdImpressions", "sessions", "conversions", "purchaseRevenue"],
        order_by_metric="advertiserAdCost", limit=50)
    if not df_ads.empty:
        st.caption(f"✨ {fetched_ads}")
        df_ads = df_ads.copy()
        df_ads["roas"] = df_ads["purchaseRevenue"] / df_ads["advertiserAdCost"].replace(0, float("nan"))
        df_ads = df_ads.sort_values("advertiserAdCost", ascending=False)
        st.dataframe(df_ads.style.format({"advertiserAdCost": "{:,.2f} €", "purchaseRevenue": "{:,.2f} €", "roas": "{:.2f}x"}), use_container_width=True)
        fig = px.bar(df_ads[df_ads["sessionGoogleAdsCampaignName"] != "(not set)"],
                     x="sessionGoogleAdsCampaignName", y=["advertiserAdCost", "purchaseRevenue"],
                     barmode="group", title="Gasto vs Revenue por campaña")
        st.plotly_chart(fig, use_container_width=True)

# ========== MERCHANT ==========
with tabs[5]:
    col_title, col_refresh = st.columns([5, 1])
    col_title.title("Google Merchant Center")
    if col_refresh.button("🔄 Refrescar", key="r_mc"):
        merchant_full_status.clear(); st.rerun()
    with st.spinner("Cargando estado del Merchant..."):
        stats, fetched_mc = merchant_full_status()
    st.caption(f"✨ {fetched_mc}")

    st.subheader("Feed legítimo (api|es|ES)")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Productos del feed", f"{stats['legitimate_total']:,}")
    col2.metric("Limpios", f"{stats['legit_clean']:,}")
    col3.metric("Con warnings", f"{stats['legit_warnings']:,}")
    col4.metric("Rechazados", f"{stats['legit_disapproved']:,}",
                delta=f"{-(413 - stats['legit_disapproved'])} vs inicio 14-may" if stats['legit_disapproved'] < 413 else None)
    if stats['legit_disapproved'] <= 20:
        st.success(f"🎯 Solo {stats['legit_disapproved']} rechazados (vs 413 al inicio del 14-may)")

    st.divider()
    st.subheader("Productos por fuente")
    df_sources = pd.DataFrame([{"fuente": k, "productos": v} for k, v in stats['sources_breakdown'].items()]).sort_values("productos", ascending=False)
    st.dataframe(df_sources, use_container_width=True)
    st.caption("Solo `api|es|ES` es feed legítimo. El resto son residuos del crawl/feed legacy.")

    st.divider()
    st.subheader("Issues más frecuentes (feed legítimo)")
    df_issues = pd.DataFrame([{"código": k, "ocurrencias": v} for k, v in stats['issues_by_code'].items()]).sort_values("ocurrencias", ascending=False).head(15)
    st.dataframe(df_issues, use_container_width=True)

# ========== ALERTAS ==========
with tabs[6]:
    st.title("🔔 Alertas")
    st.caption("Las alertas se evalúan tras cada snapshot diario (cron 03:00 Madrid). Cuando se dispara una, se envía email a los destinatarios.")

    if not db_ok:
        st.error("Postgres no disponible — las alertas no funcionan sin BD.")
    else:
        st.subheader("Nueva alerta")
        with st.form("new_alert"):
            cA, cB = st.columns(2)
            with cA:
                a_name = st.text_input("Nombre", placeholder="Ej. Caída tráfico orgánico .com")
                a_metric = st.selectbox("Métrica", list(alerts_mod.METRIC_TYPES.keys()),
                                        format_func=lambda k: alerts_mod.METRIC_TYPES[k])
                a_emails_raw = st.text_input("Email destinatarios (separados por coma)",
                                              value="cuadrado.mario@aromasdete.com")
            with cB:
                a_cond = st.selectbox("Condición", list(alerts_mod.CONDITIONS.keys()),
                                      format_func=lambda k: alerts_mod.CONDITIONS[k])
                a_threshold = st.number_input("Umbral", value=20.0, step=1.0,
                                              help="Para `lt`/`gt`: valor absoluto. Para `pct_drop/rise`: porcentaje (ej. 20 = ±20%).")
                a_window = st.selectbox("Ventana de comparación (solo para % drop/rise)",
                                         ["last_7d_avg", "last_30d_avg"],
                                         format_func=lambda k: alerts_mod.COMPARE_WINDOWS[k])
            submitted = st.form_submit_button("➕ Crear alerta", type="primary")
            if submitted:
                if not a_name:
                    st.error("Nombre obligatorio")
                else:
                    emails = [e.strip() for e in a_emails_raw.split(",") if e.strip()]
                    if not emails:
                        st.error("Al menos un email")
                    else:
                        rid = db.create_alert_rule(a_name, a_metric, a_cond, a_threshold,
                                                  a_window if a_cond.startswith("pct_") else None, emails)
                        st.success(f"Alerta #{rid} creada")
                        st.rerun()

        st.divider()
        st.subheader("Reglas configuradas")
        rules = db.list_alert_rules()
        if not rules:
            st.info("Sin reglas todavía. Crea una arriba.")
        else:
            for rule in rules:
                with st.container(border=True):
                    c1, c2, c3, c4 = st.columns([4, 2, 1, 1])
                    estado = "🟢" if rule["enabled"] else "⚪"
                    c1.markdown(f"**{estado} #{rule['id']} · {rule['name']}**")
                    metric_label = alerts_mod.METRIC_TYPES.get(rule["metric_type"], rule["metric_type"])
                    cond_label = alerts_mod.CONDITIONS.get(rule["condition"], rule["condition"])
                    c1.caption(f"`{metric_label}` {cond_label} **{rule['threshold']}** · → {', '.join(rule['notify_emails'])}")
                    if rule.get("last_evaluated_at"):
                        c2.caption(f"Última evaluación: {rule['last_evaluated_at'].strftime('%Y-%m-%d %H:%M')}")
                    if c3.button("⏸️" if rule["enabled"] else "▶️", key=f"toggle_{rule['id']}", help="Pausar/Reanudar"):
                        db.toggle_alert_rule(rule["id"], not rule["enabled"])
                        st.rerun()
                    if c4.button("🗑️", key=f"del_{rule['id']}", help="Eliminar"):
                        db.delete_alert_rule(rule["id"])
                        st.rerun()

        st.divider()
        c_eval, c_test = st.columns(2)
        if c_eval.button("🧪 Evaluar todas AHORA (sin esperar al cron)", use_container_width=True):
            with st.spinner("Evaluando..."):
                res = alerts_mod.evaluate_all_and_notify()
            for r in res:
                if r.get("triggered"):
                    st.error(f"🔔 DISPARADA #{r['rule_id']} {r['rule_name']}: {r['explanation']}")
                    if r.get("sent_to"):
                        st.caption(f"   Enviada a: {', '.join(r['sent_to'])}")
                else:
                    st.success(f"· #{r['rule_id']} {r['rule_name']}: {r.get('explanation','OK')}")

        st.divider()
        st.subheader("Historial de alertas disparadas")
        events = db.list_alert_events(limit=50)
        if not events:
            st.caption("Sin eventos todavía.")
        else:
            df_e = pd.DataFrame(events)
            st.dataframe(df_e[["triggered_at", "rule_name", "metric_value", "reference_value", "explanation", "sent_to"]],
                         use_container_width=True)


# ========== PLAN DE ACCIÓN ==========
with tabs[7]:
    st.title("Plan de acción priorizado")
    st.caption("Tags: 🤖 Claude · 🧑 Mario · 🤝 juntos · ✅ hecho")
    st.markdown("""
### ✅ Hecho (14-may-2026)
**Merchant Center**
- 596 productos basura del crawl/feed legacy borrados.
- 5 productos `illegal_drugs` reescritos.
- 5 productos `description_short` corregidos.
- 3 productos basura eliminados, 1 duplicado despublicado.

**Structured data Shopify**
- `shippingDetails` + `hasMerchantReturnPolicy` + `aggregateRating` añadidos al JSON-LD.
- Rich Results Test: 15 elementos válidos, 0 problemas no críticos.

**Limpieza theme**
- Wholesale Pricing Discount desinstalada (B2B inactivo 27 meses).
- 132 KB de código muerto eliminados del theme.

**Accesos SEO + dominios**
- `sc-domain:aromasdete.eu` añadido a GSC.
- GA4 unificado (`316499868`), cross-domain configurado.
- David Boada y Javier Casares fuera de todos los servicios.

**Dashboard**
- v0.1.0 → 0.2.0: versionado + UX de refresh.
- v0.2.0 → 0.3.0: **Postgres con histórico** + gráficos temporales.

### ⏳ Pendiente
- 🧑 Chus debe decidir las 10 fotos faltantes (email enviado).
- 🤝 Esperar 24-48h y verificar estrellas en SERP.
- 🤝 Auditoría SEO orgánica del blog Shopify (objetivo inicial pendiente).
""")

# ========== CHANGELOG ==========
with tabs[8]:
    st.title("📋 Changelog")
    st.caption(f"Versión actual: **v{__version__}** · Released {RELEASE_DATE}")
    for path in ["/app/CHANGELOG.md", "CHANGELOG.md"]:
        try:
            with open(path) as f:
                st.markdown(f.read())
            break
        except FileNotFoundError:
            continue
    else:
        st.warning("CHANGELOG.md no encontrado")

st.divider()
st.caption(f"SEO Aromas v{__version__} · {RELEASE_DATE} · Postgres · noindex · [github](https://github.com/inhumario/aromas-seo-dashboard)")
