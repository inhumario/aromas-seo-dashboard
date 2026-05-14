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
CACHE_TTL = 60 * 15  # 15 min default

# ---------- Helpers ----------
def now_es():
    return datetime.now(timezone(timedelta(hours=2))).strftime("%Y-%m-%d %H:%M:%S")

def show_data_status(cache_hit: bool, fetched_at: str, fn_label: str):
    """Indicador de frescura de datos por sección."""
    if cache_hit:
        st.caption(f"💾 Datos del caché ({fn_label}) cargados a las {fetched_at}. Refresca para obtener datos frescos de la API.")
    else:
        st.caption(f"✨ Datos frescos ({fn_label}) cargados a las {fetched_at}.")

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def gsc_query(site_url, start, end, dimensions, row_limit=1000):
    res = clients["gsc"].searchanalytics().query(siteUrl=site_url, body={
        "startDate": start, "endDate": end,
        "dimensions": dimensions, "rowLimit": row_limit,
    }).execute()
    rows = res.get("rows", [])
    if not rows:
        return pd.DataFrame(), now_es()
    df = pd.DataFrame([{
        **{d: r["keys"][i] for i, d in enumerate(dimensions)},
        "clicks": r["clicks"],
        "impressions": r["impressions"],
        "ctr": r["ctr"],
        "position": r["position"],
    } for r in rows])
    return df, now_es()

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
        return pd.DataFrame(), now_es()
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
    return pd.DataFrame(data), now_es()

@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def merchant_full_status():
    """Saca productos clasificados por feed (legítimo api|es|ES vs crawl/legacy)."""
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

    # Estados
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
    return stats, now_es()

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

    st.caption(f"TTL del caché: {CACHE_TTL // 60} min · Si una métrica no refleja un cambio reciente, pulsa Refrescar.")

    st.divider()
    st.caption("**Latencia de las fuentes:**")
    st.caption("· GSC: ~48 h de retraso real (Google publica con delay)")
    st.caption("· GA4: ~5-15 min para reports, realtime instantáneo")
    st.caption("· Merchant: cuasi tiempo real (1-3 h por sync)")

    st.divider()
    st.caption(f"[GitHub](https://github.com/inhumario/aromas-seo-dashboard)")

S = str(start_date); E = str(end_date)

# ---------- Tabs ----------
tabs = st.tabs([
    "🍵 Resumen",
    "🔍 SEO orgánico",
    "📊 GA4",
    "💰 Google Ads",
    "🛒 Merchant",
    "🎯 Plan de acción",
    "📋 Changelog",
])

# ========== RESUMEN ==========
with tabs[0]:
    col_title, col_refresh = st.columns([5, 1])
    col_title.title("Resumen general")
    if col_refresh.button("🔄 Refrescar", key="r_resumen"):
        gsc_query.clear()
        ga4_report.clear()
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
            col4.metric("ROAS Ads", f"{rev_ads / cost:.2f}x", help=f"{cost:,.0f} € gastado → {rev_ads:,.0f} € revenue")

    st.caption(f"✨ Datos GA4 cargados a las {fetched}")
    st.divider()

    st.subheader("Tráfico por hostname (GA4)")
    if not df_hosts.empty:
        st.dataframe(df_hosts.sort_values("sessions", ascending=False), use_container_width=True)

# ========== SEO ORGÁNICO ==========
with tabs[1]:
    col_title, col_refresh = st.columns([5, 1])
    col_title.title("SEO orgánico — Search Console")
    if col_refresh.button("🔄 Refrescar", key="r_seo"):
        gsc_query.clear()
        st.rerun()
    st.caption(f"Periodo: {S} → {E}")

    site = st.selectbox("Propiedad", ["sc-domain:aromasdete.com", "sc-domain:aromasdete.eu"])
    df_q, fetched_q = gsc_query(site, S, E, ["query"], row_limit=2000)
    df_p, fetched_p = gsc_query(site, S, E, ["page"], row_limit=2000)

    if df_q.empty:
        st.info("Sin datos. Si la propiedad es nueva, GSC tarda 2-3 días en mostrar primeros datos.")
    else:
        st.caption(f"✨ Datos cargados a las {fetched_q}")

        st.subheader("Oportunidades — queries con impresiones altas y posición rescatable (5-20)")
        opp = df_q[(df_q["impressions"] >= 100) & (df_q["position"] >= 5) & (df_q["position"] <= 20)].sort_values("impressions", ascending=False)
        st.caption(f"{len(opp)} queries con potencial (≥100 impresiones y posición 5-20)")
        st.dataframe(
            opp.head(50).style.format({"ctr": "{:.2%}", "position": "{:.1f}"}),
            use_container_width=True,
        )

        st.divider()
        st.subheader("Top 50 queries por clicks")
        st.dataframe(
            df_q.sort_values("clicks", ascending=False).head(50).style.format({"ctr": "{:.2%}", "position": "{:.1f}"}),
            use_container_width=True,
        )

        st.divider()
        st.subheader("Top 50 páginas por clicks")
        st.dataframe(
            df_p.sort_values("clicks", ascending=False).head(50).style.format({"ctr": "{:.2%}", "position": "{:.1f}"}),
            use_container_width=True,
        )

# ========== GA4 ==========
with tabs[2]:
    col_title, col_refresh = st.columns([5, 1])
    col_title.title("GA4 — analítica")
    if col_refresh.button("🔄 Refrescar", key="r_ga4"):
        ga4_report.clear()
        st.rerun()
    st.caption(f"Periodo: {S} → {E}")

    df_pp, fetched_pp = ga4_report(S, E,
        ["hostName", "pagePath"],
        ["sessions", "engagedSessions", "screenPageViews", "purchaseRevenue"],
        order_by_metric="sessions", limit=100)
    if not df_pp.empty:
        st.caption(f"✨ Datos cargados a las {fetched_pp}")
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
with tabs[3]:
    col_title, col_refresh = st.columns([5, 1])
    col_title.title("Google Ads (datos vía GA4)")
    if col_refresh.button("🔄 Refrescar", key="r_ads"):
        ga4_report.clear()
        st.rerun()
    st.caption(f"Periodo: {S} → {E}")

    df_ads, fetched_ads = ga4_report(S, E,
        ["sessionGoogleAdsCampaignName"],
        ["advertiserAdCost", "advertiserAdClicks", "advertiserAdImpressions", "sessions", "conversions", "purchaseRevenue"],
        order_by_metric="advertiserAdCost", limit=50)
    if not df_ads.empty:
        st.caption(f"✨ Datos cargados a las {fetched_ads}")
        df_ads = df_ads.copy()
        df_ads["roas"] = df_ads["purchaseRevenue"] / df_ads["advertiserAdCost"].replace(0, float("nan"))
        df_ads = df_ads.sort_values("advertiserAdCost", ascending=False)
        st.dataframe(
            df_ads.style.format({
                "advertiserAdCost": "{:,.2f} €",
                "purchaseRevenue": "{:,.2f} €",
                "roas": "{:.2f}x",
            }),
            use_container_width=True,
        )
        fig = px.bar(df_ads[df_ads["sessionGoogleAdsCampaignName"] != "(not set)"],
                     x="sessionGoogleAdsCampaignName",
                     y=["advertiserAdCost", "purchaseRevenue"],
                     barmode="group",
                     title="Gasto vs Revenue por campaña")
        st.plotly_chart(fig, use_container_width=True)

# ========== MERCHANT ==========
with tabs[4]:
    col_title, col_refresh = st.columns([5, 1])
    col_title.title("Google Merchant Center")
    if col_refresh.button("🔄 Refrescar", key="r_mc"):
        merchant_full_status.clear()
        st.rerun()

    with st.spinner("Cargando estado del Merchant Center..."):
        stats, fetched_mc = merchant_full_status()
    st.caption(f"✨ Datos cargados a las {fetched_mc}")

    st.subheader("Feed legítimo (api|es|ES)")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Productos del feed", f"{stats['legitimate_total']:,}")
    col2.metric("Limpios", f"{stats['legit_clean']:,}")
    col3.metric("Con warnings", f"{stats['legit_warnings']:,}")
    col4.metric("Rechazados", f"{stats['legit_disapproved']:,}",
                delta=f"{-(413 - stats['legit_disapproved'])} vs inicio 14-may" if stats['legit_disapproved'] < 413 else None)

    if stats['legit_disapproved'] <= 20:
        st.success(f"🎯 Solo {stats['legit_disapproved']} productos rechazados (vs 413 al inicio del 14-may)")

    st.divider()
    st.subheader("Desglose de todos los productos por fuente")
    df_sources = pd.DataFrame([
        {"fuente": k, "productos": v} for k, v in stats['sources_breakdown'].items()
    ]).sort_values("productos", ascending=False)
    st.dataframe(df_sources, use_container_width=True)
    st.caption("Solo `api|es|ES` (Content API del canal Google & YouTube oficial) es feed legítimo. El resto (crawl, feed legacy) son residuos o tráfico de otros mercados.")

    st.divider()
    st.subheader("Issues más frecuentes (solo feed legítimo)")
    df_issues = pd.DataFrame([
        {"código": k, "ocurrencias": v} for k, v in stats['issues_by_code'].items()
    ]).sort_values("ocurrencias", ascending=False).head(15)
    st.dataframe(df_issues, use_container_width=True)
    st.caption("Recuerda: cada producto cuenta su issue × destinos (Shopping, DisplayAds, SurfacesAcrossGoogle). Para # productos únicos, dividir entre 3 aprox.")

# ========== PLAN DE ACCIÓN ==========
with tabs[5]:
    st.title("Plan de acción priorizado")
    st.caption("Tags: 🤖 lo hace Claude · 🧑 lo hace Mario · 🤝 lo hacemos juntos · ✅ ya hecho")

    st.markdown("""
### ✅ Ya hechos (14-may-2026)

**Limpieza Merchant Center**
- ✅ 596 productos basura del crawl/feed legacy borrados de Merchant Center.
- ✅ "Encontrado por Google" deshabilitado en Merchant.
- ✅ 5 productos con `illegal_drugs_policy_violation` con descripciones reescritas.
- ✅ 5 productos con `description_short` corregidas (SEO description en cafés y tazas).
- ✅ 3 productos basura eliminados de Shopify (PRODUCTO DE PRUEBA, Pack Oferta Tienda x2).
- ✅ 1 producto duplicado despublicado (Café de Pistacho "-copia").

**Structured data en Shopify**
- ✅ `shippingDetails` añadido al JSON-LD de cada producto (tarifas por país).
- ✅ `hasMerchantReturnPolicy` añadido (14 días, ReturnByMail).
- ✅ `aggregateRating` añadido a 103 productos con ≥3 reseñas Trusted Shops.
- ✅ Rich Results Test: 15 elementos válidos, 0 problemas no críticos.

**Limpieza theme Shopify**
- ✅ App Wholesale Pricing Discount desinstalada (canal B2B inactivo 27 meses).
- ✅ 4 archivos del plugin borrados del theme.
- ✅ 11 archivos del theme limpiados de hooks `data-wpd-*` y clases `data-wpd-hide`.
- ✅ Theme renombrado a "Aromasdete - Producción 2026-05-14".

**Accesos SEO**
- ✅ OAuth Google: GSC + GA4 + Merchant + Site Verification operativos.
- ✅ `sc-domain:aromasdete.eu` añadido a Search Console.
- ✅ GA4 unificado en una sola propiedad (316499868) midiendo .com + .eu + blog + noticias.
- ✅ Cross-domain configurado entre los 4 dominios.
- ✅ David Boada y Javier Casares fuera de Merchant + GSC.

**WordPress (blog. y noticias.)**
- ✅ Site Kit + The SEO Framework activos.
- ✅ Tag GA4 cargando con el measurement de la tienda principal.

### ⏳ Pendientes de Chus / equipo

- 🧑 **10 productos sin foto en Shopify** → email enviado a Chus (`mjperez@aromasdete.com`) para decidir foto o despublicar.

### 🟡 Próximos pasos sugeridos

- 🤖 **Esperar 24-48h** → Google reindexa con los cambios. Estrellas en SERP deberían empezar a aparecer.
- 🤝 **Auditoría SEO orgánica del blog Shopify** → identificar posts que merece la pena refrescar (objetivo original de la sesión).
- 🤝 **Reactivar blog.aromasdete.com y noticias.aromasdete.com** → 991 posts dormidos desde 2023 con tráfico orgánico activo.

### 🟢 Información

- 🟢 **El canal B2B lleva 27 meses inactivo**: 652 clientes con tag B2B/wholesale, último pedido grupal febrero 2024. Si quieres reactivar, mejor con Shopify Markets B2B nativo.
""")

# ========== CHANGELOG ==========
with tabs[6]:
    st.title("📋 Changelog")
    st.caption(f"Versión actual: **v{__version__}** · Released {RELEASE_DATE}")
    try:
        with open("/app/CHANGELOG.md") as f:
            changelog = f.read()
    except FileNotFoundError:
        try:
            with open("CHANGELOG.md") as f:
                changelog = f.read()
        except FileNotFoundError:
            changelog = "_CHANGELOG.md no encontrado en el contenedor_"
    st.markdown(changelog)

# ---------- Footer ----------
st.divider()
st.caption(f"SEO Aromas v{__version__} · {RELEASE_DATE} · noindex · [github](https://github.com/inhumario/aromas-seo-dashboard)")
