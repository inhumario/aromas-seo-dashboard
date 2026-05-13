import os
import json
import base64
from datetime import date, timedelta

import streamlit as st
import pandas as pd
import plotly.express as px
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

# ---------- Config Streamlit ----------
st.set_page_config(
    page_title="SEO Aromas",
    page_icon="🍵",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Inyectar noindex en cada respuesta HTML
st.markdown(
    '<meta name="robots" content="noindex, nofollow, noarchive, nosnippet">'
    '<meta name="googlebot" content="noindex, nofollow">',
    unsafe_allow_html=True,
)

# ---------- Auth simple por contraseña ----------
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

# ---------- Cargar credenciales Google ----------
@st.cache_resource
def get_creds():
    b64 = os.environ.get("GOOGLE_TOKEN_B64", "")
    if not b64:
        st.error("Falta GOOGLE_TOKEN_B64 en variables de entorno")
        st.stop()
    data = json.loads(base64.b64decode(b64))
    # Escribir a un archivo temporal para que la lib pueda cargarlo
    tmp = "/tmp/google_token.json"
    with open(tmp, "w") as f:
        json.dump(data, f)
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

# ---------- Selector de fechas global ----------
st.sidebar.header("Periodo")
periodo = st.sidebar.radio("Días", [7, 28, 90, 180], index=2, format_func=lambda d: f"Últimos {d} días")
end_date = date.today() - timedelta(days=2)
start_date = end_date - timedelta(days=periodo - 1)
st.sidebar.write(f"{start_date} → {end_date}")
st.sidebar.divider()
if st.sidebar.button("Limpiar caché"):
    st.cache_data.clear()
    st.rerun()

# ---------- Helpers con caché ----------
@st.cache_data(ttl=3600)
def gsc_query(site_url: str, start: str, end: str, dimensions: list, row_limit: int = 1000):
    res = clients["gsc"].searchanalytics().query(siteUrl=site_url, body={
        "startDate": start, "endDate": end,
        "dimensions": dimensions, "rowLimit": row_limit,
    }).execute()
    rows = res.get("rows", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([{
        **{d: r["keys"][i] for i, d in enumerate(dimensions)},
        "clicks": r["clicks"],
        "impressions": r["impressions"],
        "ctr": r["ctr"],
        "position": r["position"],
    } for r in rows])
    return df

@st.cache_data(ttl=3600)
def ga4_report(start: str, end: str, dimensions: list, metrics: list, order_by_metric: str = None, limit: int = 1000):
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
        return pd.DataFrame()
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
    return pd.DataFrame(data)

@st.cache_data(ttl=3600)
def merchant_summary():
    products_iss = []
    req = clients["mc"].productstatuses().list(merchantId=MERCHANT_ID, maxResults=250)
    total = 0
    while req:
        r = req.execute()
        for p in r.get("resources", []):
            issues = p.get("itemLevelIssues", [])
            disapproved = any(
                d.get("status") == "disapproved"
                for d in p.get("destinationStatuses", [])
            )
            if issues:
                products_iss.append({
                    "productId": p.get("productId"),
                    "title": p.get("title"),
                    "link": p.get("link"),
                    "issues_count": len(issues),
                    "top_issue": (issues[0].get("description") or issues[0].get("code")) if issues else None,
                    "disapproved": disapproved,
                })
            total += 1
        req = clients["mc"].productstatuses().list_next(req, r)
    return total, pd.DataFrame(products_iss)

# ---------- Tabs ----------
tabs = st.tabs([
    "🍵 Resumen",
    "🔍 SEO orgánico",
    "📊 GA4",
    "💰 Google Ads",
    "🛒 Merchant",
    "🎯 Plan de acción",
])
S = str(start_date); E = str(end_date)

# ========== RESUMEN ==========
with tabs[0]:
    st.title("Resumen — Aromas SEO/Marketing")
    st.caption(f"Periodo: {S} → {E}")

    col1, col2, col3 = st.columns(3)

    # GA4 totales
    df_hosts = ga4_report(S, E,
        ["hostName"],
        ["sessions", "totalUsers", "screenPageViews", "purchaseRevenue", "transactions"])
    if not df_hosts.empty:
        total_sessions = int(df_hosts["sessions"].sum())
        total_users = int(df_hosts["totalUsers"].sum())
        total_revenue = float(df_hosts["purchaseRevenue"].sum())
        total_tx = int(df_hosts["transactions"].sum())
        col1.metric("Sesiones", f"{total_sessions:,}")
        col2.metric("Revenue", f"{total_revenue:,.0f} €")
        col3.metric("Pedidos", f"{total_tx:,}")

    st.divider()

    col1, col2 = st.columns(2)

    # GSC totales .com
    df_gsc = gsc_query("sc-domain:aromasdete.com", S, E, ["query"], row_limit=25000)
    if not df_gsc.empty:
        total_clicks = int(df_gsc["clicks"].sum())
        total_impr = int(df_gsc["impressions"].sum())
        avg_pos = (df_gsc["position"] * df_gsc["impressions"]).sum() / df_gsc["impressions"].sum()
        col1.metric("Clicks orgánicos (GSC)", f"{total_clicks:,}")
        col1.metric("Impresiones orgánicas", f"{total_impr:,}")
        col1.metric("Posición media", f"{avg_pos:.2f}")

    # GA4 Ads
    df_ads = ga4_report(S, E,
        ["sessionGoogleAdsCampaignName"],
        ["advertiserAdCost", "advertiserAdClicks", "sessions", "purchaseRevenue"],
        order_by_metric="advertiserAdCost", limit=50)
    if not df_ads.empty:
        total_cost = float(df_ads["advertiserAdCost"].sum())
        ads_revenue = float(df_ads["purchaseRevenue"].sum())
        col2.metric("Gasto Google Ads", f"{total_cost:,.0f} €")
        col2.metric("Revenue desde Ads", f"{ads_revenue:,.0f} €")
        if total_cost > 0:
            col2.metric("ROAS", f"{ads_revenue/total_cost:.2f}x")

    st.divider()

    st.subheader("Tráfico por hostname")
    if not df_hosts.empty:
        st.dataframe(df_hosts.sort_values("sessions", ascending=False), use_container_width=True)
        fig = px.bar(df_hosts.sort_values("sessions", ascending=True), x="sessions", y="hostName", orientation="h")
        st.plotly_chart(fig, use_container_width=True)

# ========== SEO ORGÁNICO ==========
with tabs[1]:
    st.title("SEO orgánico — Search Console")
    st.caption(f"Periodo: {S} → {E}")

    site = st.selectbox("Propiedad", ["sc-domain:aromasdete.com", "sc-domain:aromasdete.eu"])

    df_q = gsc_query(site, S, E, ["query"], row_limit=2000)
    df_p = gsc_query(site, S, E, ["page"], row_limit=2000)

    if df_q.empty:
        st.info("Sin datos para esta propiedad en este periodo. La propiedad `.eu` se creó hoy, GSC tarda 2-3 días en mostrar primeros datos.")
    else:
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
    st.title("GA4 — analítica")
    st.caption(f"Periodo: {S} → {E}")

    st.subheader("Top 30 páginas por sesiones")
    df_pp = ga4_report(S, E,
        ["hostName", "pagePath"],
        ["sessions", "engagedSessions", "screenPageViews", "purchaseRevenue"],
        order_by_metric="sessions", limit=100)
    if not df_pp.empty:
        df_pp["url"] = "https://" + df_pp["hostName"] + df_pp["pagePath"]
        st.dataframe(df_pp.head(30), use_container_width=True)

    st.divider()
    st.subheader("Source / Medium")
    df_sm = ga4_report(S, E,
        ["sessionSource", "sessionMedium"],
        ["sessions", "engagedSessions", "conversions", "purchaseRevenue"],
        order_by_metric="sessions", limit=30)
    if not df_sm.empty:
        st.dataframe(df_sm, use_container_width=True)

# ========== ADS ==========
with tabs[3]:
    st.title("Google Ads (datos vía GA4)")
    st.caption(f"Periodo: {S} → {E}")
    if not df_ads.empty:
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
    st.title("Google Merchant Center")
    with st.spinner("Cargando productos de Merchant…"):
        total, df_iss = merchant_summary()
    col1, col2, col3 = st.columns(3)
    col1.metric("Productos totales", f"{total:,}")
    col2.metric("Con issues", f"{len(df_iss):,}")
    if not df_iss.empty:
        disap = int(df_iss["disapproved"].sum())
        col3.metric("Rechazados", f"{disap:,}")

        st.divider()
        st.subheader("Tipos de issues más comunes")
        top_issues = df_iss["top_issue"].value_counts().head(15)
        st.bar_chart(top_issues)

        st.divider()
        st.subheader("Productos con issues")
        st.dataframe(df_iss, use_container_width=True)

# ========== PLAN DE ACCIÓN ==========
with tabs[5]:
    st.title("Plan de acción priorizado")
    st.caption("Tags: 🤖 lo hace Claude · 🧑 lo hace Mario · 🤝 lo hacemos juntos")

    st.markdown("""
### Prioridad ALTA

**🛒 Limpieza masiva de Merchant Center** 🤝
- Hay un porcentaje altísimo de productos con issues (1.596 de 1.808 = ~88%).
- Antes de optimizar SEO, esto se debe arreglar: cada producto rechazado pierde visibilidad en Shopping y Free Listings.
- Plan: descargar el listado, agrupar por tipo de issue, resolver los 5 más frecuentes (suelen ser GTIN, brand, image, price mismatch).

**🔍 Oportunidades GSC — pos 5-20 con >100 impresiones** 🤖
- Identificadas en la pestaña "SEO orgánico".
- Plan: yo genero un artículo dedicado por cada keyword sin landing optimizada usando el corpus Boada. Tú lo revisas y lo publicas.

**🇮🇹 Aromasdete.eu sin datos en GSC** 🤖
- La propiedad se acaba de añadir. En 1-2 semanas tendrá datos.
- Plan: yo paso una segunda auditoría del .eu cuando haya >7 días de datos.

### Prioridad MEDIA

**📝 Reactivar blog.aromasdete.com y noticias.aromasdete.com** 🤝
- 991 posts dormidos desde 2023. Aún reciben impresiones orgánicas.
- Plan: yo audito qué posts traen tráfico, propongo cuáles refrescar/redirigir 301 a la tienda. Tú decides el alcance.

**🔗 Cross-domain ya configurado** ✅
- Sesiones blog → tienda unificadas. Nada que hacer.

**📈 Campaña PMax Cafes con ROAS bajo** 🧑
- Si está confirmado por márgenes, puede no ser rentable. Revisar con el responsable de Ads.

### Prioridad BAJA

**🧹 Token de propiedad no utilizado en GSC** 🧑
- Aparece un (1) token sin uso en la pantalla de Verificación de propiedad. No urgente, pero conviene limpiar.

**🔁 CNAME hermano `qg7pvwynso34.aromasdete.com`** 🧑
- Otro token de verificación de algún propietario antiguo. No tocado en la limpieza de hoy. Si quieres, lo revisamos.

**🛍️ Merchant Center para `.eu`** 🤝
- Si quieres vender por Google Shopping en Italia, hay que añadir feed multi-país o crear una segunda cuenta Merchant.

---

### Lo que YA está hecho ✅

- OAuth a Google APIs (GSC + GA4 + Merchant + Site Verification) operativo.
- Search Console: `sc-domain:aromasdete.com` + `sc-domain:aromasdete.eu` verificados.
- GA4 unificado en una sola propiedad (316499868) midiendo .com + .eu + blog. + noticias.
- Cross-domain entre los 4 dominios configurado.
- Limpieza de accesos: David Boada fuera de Merchant + GSC + GA4 link MCC. Javier Casares fuera de GSC.
- WP blog/noticias con Site Kit + The SEO Framework activos.
- Tag GA4 (Google Tag GT-PLTTFN3N) cargando correctamente en los 4 dominios.
""")

    st.divider()
    with st.expander("Glosario y notas técnicas"):
        st.markdown("""
- **CTR**: clicks / impresiones en GSC.
- **Posición media GSC**: posición promedio en SERP. <10 = primera página.
- **ROAS**: revenue / gasto en Ads. >3x suele ser sano para ecommerce con margen 30-40%.
- **(not set)** en source/medium: tráfico que GA4 no pudo atribuir (directo, sin referrer válido, etc.).
- **Cross-domain**: cuando un visitante navega entre `.com`, `.eu`, `blog.`, `noticias.`, mantiene la misma sesión.
""")
