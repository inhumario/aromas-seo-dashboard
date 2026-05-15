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
import auth

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

# ---------- Estilo: oculta el cromo de Streamlit y pule la interfaz ----------
st.markdown("""
<style>
  #MainMenu, [data-testid="stToolbar"], [data-testid="stDecoration"],
  [data-testid="stStatusWidget"] { display: none !important; }
  footer { visibility: hidden; height: 0; }
  .stButton button { border-radius: 8px; font-weight: 600; }
  .block-container { padding-top: 2.4rem; }
</style>
""", unsafe_allow_html=True)

APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://seo.aromasdete.com").rstrip("/")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
GA4_PROPERTY = "properties/316499868"
MERCHANT_ID = "115390048"
CACHE_TTL = 60 * 15

# ---------- Inicialización de base de datos ----------
@st.cache_resource
def init_database():
    try:
        db.init_db()
        return True
    except Exception:
        return False

db_ok = init_database()

@st.cache_resource
def bootstrap_auth():
    """Crea el administrador inicial si la tabla de usuarios está vacía."""
    try:
        auth.init_auth()
        return True
    except Exception as e:
        return str(e)

if db_ok:
    bootstrap_auth()


# ====================================================================
#  AUTENTICACIÓN  (usuario + contraseña, multi-usuario, en Postgres)
# ====================================================================

def _auth_layout_css():
    """Oculta el sidebar y centra el contenido en las pantallas de acceso."""
    st.markdown("""
    <style>
      [data-testid="stSidebar"] { display: none !important; }
      .block-container { max-width: 460px; padding-top: 3.2rem; }
    </style>
    """, unsafe_allow_html=True)


def _auth_header(subtitle):
    st.markdown(
        "<div style='text-align:center;font-size:3.4rem;line-height:1'>🍵</div>"
        "<h1 style='text-align:center;font-size:1.6rem;margin:.4rem 0 0'>Panel SEO · Aromas de Té</h1>"
        f"<p style='text-align:center;color:#8A7E6C;margin:.25rem 0 1.3rem'>{subtitle}</p>",
        unsafe_allow_html=True,
    )


def _pop_flash():
    msg = st.session_state.pop("flash", None)
    if msg:
        getattr(st, msg[0])(msg[1])


def render_login_screen():
    _auth_layout_css()
    _auth_header("Analítica de posicionamiento y rendimiento")
    _pop_flash()
    with st.container(border=True):
        tab_login, tab_recover = st.tabs(["Iniciar sesión", "He olvidado mi contraseña"])
        with tab_login:
            with st.form("login_form"):
                login = st.text_input("Usuario o email", placeholder="tu.usuario")
                pwd = st.text_input("Contraseña", type="password")
                submit = st.form_submit_button("Entrar", type="primary", use_container_width=True)
            if submit:
                user = auth.authenticate(login, pwd)
                if user:
                    st.session_state.auth_user = user
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos.")
        with tab_recover:
            st.caption("Te enviaremos un enlace a tu email para crear una contraseña nueva.")
            with st.form("recover_form"):
                rlogin = st.text_input("Tu usuario o email")
                rsubmit = st.form_submit_button("Enviar enlace de recuperación",
                                                use_container_width=True)
            if rsubmit:
                auth.request_password_reset_public(rlogin, APP_BASE_URL)
                st.success("Si la cuenta existe, recibirás un correo con instrucciones en unos "
                           "minutos. Revisa también la carpeta de spam.")
    st.markdown(
        f"<div style='text-align:center;color:#A99F8C;font-size:.8rem;margin-top:1rem'>"
        f"Uso interno · v{__version__}</div>", unsafe_allow_html=True)


def render_reset_screen(token):
    _auth_layout_css()
    _auth_header("Crear una contraseña nueva")
    user, reason = auth.check_reset_token(token)
    with st.container(border=True):
        if not user:
            st.error(reason)
            if st.button("Volver al inicio", use_container_width=True):
                st.query_params.clear()
                st.rerun()
            return
        st.markdown(f"Cuenta: **{user['username']}** · {user['email']}")
        with st.form("reset_form"):
            p1 = st.text_input("Nueva contraseña", type="password", help="Mínimo 8 caracteres.")
            p2 = st.text_input("Repite la contraseña", type="password")
            submit = st.form_submit_button("Guardar contraseña", type="primary",
                                           use_container_width=True)
        if submit:
            if p1 != p2:
                st.error("Las contraseñas no coinciden.")
            else:
                ok, msg = auth.reset_password_with_token(token, p1)
                if ok:
                    st.session_state["flash"] = ("success", msg)
                    st.query_params.clear()
                    st.rerun()
                else:
                    st.error(msg)


def render_force_change_screen(user):
    _auth_layout_css()
    _auth_header("Establece tu contraseña")
    with st.container(border=True):
        st.info("Por seguridad, antes de continuar debes establecer una contraseña personal.")
        with st.form("force_form"):
            p1 = st.text_input("Nueva contraseña", type="password", help="Mínimo 8 caracteres.")
            p2 = st.text_input("Repite la contraseña", type="password")
            submit = st.form_submit_button("Guardar y entrar", type="primary",
                                           use_container_width=True)
        if submit:
            if p1 != p2:
                st.error("Las contraseñas no coinciden.")
            else:
                ok, msg = auth.force_set_password(user["id"], p1)
                if ok:
                    user["must_change_password"] = False
                    st.session_state.auth_user = user
                    st.rerun()
                else:
                    st.error(msg)


def render_legacy_login():
    """Acceso de emergencia cuando Postgres no está disponible."""
    _auth_layout_css()
    _auth_header("Acceso temporal")
    with st.container(border=True):
        st.warning("La base de datos de usuarios no está disponible. Acceso de emergencia con "
                   "la contraseña general.")
        with st.form("legacy_form"):
            pwd = st.text_input("Contraseña", type="password")
            submit = st.form_submit_button("Entrar", type="primary", use_container_width=True)
        if submit:
            if DASHBOARD_PASSWORD and pwd == DASHBOARD_PASSWORD:
                st.session_state.auth_user = {
                    "id": None, "username": "admin", "email": "",
                    "full_name": "Administrador", "role": "admin",
                    "is_active": True, "must_change_password": False, "_legacy": True,
                }
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")


def run_auth_gate():
    # 1) Enlace de recuperación de contraseña
    token = st.query_params.get("reset_token")
    if token:
        if not db_ok:
            _auth_layout_css()
            _auth_header("Recuperación no disponible")
            st.error("La base de datos no está disponible ahora mismo. Inténtalo más tarde.")
            st.stop()
        render_reset_screen(token)
        st.stop()

    # 2) Postgres caído → acceso de emergencia con contraseña general
    if not db_ok:
        if not st.session_state.get("auth_user"):
            render_legacy_login()
            st.stop()
        return st.session_state.auth_user

    # 3) Sesión activa
    user = st.session_state.get("auth_user")
    if not user:
        render_login_screen()
        st.stop()

    # 4) Cambio de contraseña obligatorio en el primer acceso
    if user.get("must_change_password"):
        render_force_change_screen(user)
        st.stop()

    return user


USER = run_auth_gate()


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
    with st.container(border=True):
        _rol = "Administrador" if USER.get("role") == "admin" else "Visualización"
        st.markdown(f"**{USER.get('full_name') or USER.get('username')}**")
        st.caption(f"{USER.get('username')} · {_rol}")
        if st.button("Cerrar sesión", use_container_width=True, key="logout_btn"):
            st.session_state.pop("auth_user", None)
            st.rerun()

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
    st.caption("Panel SEO · Aromas de Té · uso interno")

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
    "👤 Cuenta",
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

# ========== CUENTA ==========
with tabs[9]:
    st.title("👤 Cuenta")
    legacy = USER.get("_legacy", False)
    rol_label = "Administrador" if USER.get("role") == "admin" else "Visualización"

    st.subheader("Mi perfil")
    c1, c2, c3 = st.columns(3)
    c1.metric("Usuario", USER.get("username") or "—")
    c2.metric("Rol", rol_label)
    c3.metric("Email", USER.get("email") or "—")

    if legacy:
        st.info("Estás en modo de acceso de emergencia (sin conexión a la base de datos). "
                "El cambio de contraseña y la gestión de usuarios no están disponibles ahora mismo.")
    else:
        st.divider()
        st.subheader("Cambiar mi contraseña")
        with st.form("change_pw_form"):
            cur_pw = st.text_input("Contraseña actual", type="password")
            np1 = st.text_input("Nueva contraseña", type="password", help="Mínimo 8 caracteres.")
            np2 = st.text_input("Repite la nueva contraseña", type="password")
            pw_ok = st.form_submit_button("Actualizar contraseña", type="primary")
        if pw_ok:
            if np1 != np2:
                st.error("Las contraseñas nuevas no coinciden.")
            else:
                done, msg = auth.change_own_password(USER["id"], cur_pw, np1)
                if done:
                    st.success(msg)
                else:
                    st.error(msg)

    # ---- Gestión de usuarios (solo administradores) ----
    if USER.get("role") == "admin" and not legacy:
        st.divider()
        st.subheader("Gestión de usuarios")
        st.caption("Crea cuentas para el equipo o para clientes. Cada persona accede con su propio "
                   "usuario y contraseña; las contraseñas se guardan cifradas, nunca en texto plano.")

        _gf = st.session_state.pop("acct_flash", None)
        if _gf:
            getattr(st, _gf[0])(_gf[1])

        with st.expander("➕ Crear nuevo usuario", expanded=False):
            with st.form("create_user_form"):
                cu1, cu2 = st.columns(2)
                with cu1:
                    nu_user = st.text_input("Usuario", placeholder="nombre.apellido")
                    nu_name = st.text_input("Nombre completo", placeholder="Nombre Apellido")
                with cu2:
                    nu_email = st.text_input("Email")
                    nu_role = st.selectbox(
                        "Rol", ["viewer", "admin"],
                        format_func=lambda r: "Visualización" if r == "viewer" else "Administrador")
                nu_invite = st.checkbox(
                    "Enviar email de bienvenida con un enlace para que configure su contraseña",
                    value=True)
                nu_pw = st.text_input(
                    "Contraseña inicial (opcional)", type="password",
                    help="Déjala vacía para generar una automáticamente. El usuario deberá "
                         "cambiarla en su primer acceso.")
                create_ok = st.form_submit_button("Crear usuario", type="primary")
            if create_ok:
                pw = (nu_pw or "").strip()
                generated = False
                if not pw:
                    pw = auth.generate_password()
                    generated = True
                done, msg = auth.create_user(nu_user, nu_email, nu_name, pw,
                                             role=nu_role, must_change=True)
                if not done:
                    st.error(msg)
                else:
                    if nu_invite:
                        row = auth.get_user_by_login(nu_email)
                        sent, send_msg = auth.send_reset_link(row, APP_BASE_URL, welcome=True)
                        if sent:
                            st.session_state["acct_flash"] = (
                                "success", f"Usuario creado. Email de bienvenida enviado a {nu_email}.")
                        else:
                            st.session_state["acct_flash"] = (
                                "warning", f"Usuario creado, pero no se pudo enviar el email "
                                           f"({send_msg}). Contraseña temporal: «{pw}» — "
                                           "compártela de forma segura.")
                    elif generated:
                        st.session_state["acct_flash"] = (
                            "success", f"Usuario creado. Contraseña temporal: «{pw}» — compártela "
                                       "de forma segura; deberá cambiarla al entrar.")
                    else:
                        st.session_state["acct_flash"] = (
                            "success", "Usuario creado con la contraseña indicada. Deberá "
                                       "cambiarla en su primer acceso.")
                    st.rerun()

        st.markdown("**Usuarios con acceso**")
        for u in auth.list_users():
            with st.container(border=True):
                es_yo = u["id"] == USER["id"]
                u_rol = "Administrador" if u["role"] == "admin" else "Visualización"
                u_estado = "🟢 Activo" if u["is_active"] else "⚪ Inactivo"
                u_last = (u["last_login_at"].strftime("%Y-%m-%d %H:%M")
                          if u.get("last_login_at") else "nunca")
                st.markdown(f"**{u['full_name'] or u['username']}**"
                            + ("  ·  *(tú)*" if es_yo else ""))
                st.caption(f"`{u['username']}` · {u['email']}")
                st.caption(f"{u_rol} · {u_estado} · último acceso: {u_last}")

                bcol1, bcol2, bcol3, bcol4 = st.columns(4)
                # Rol
                if u["role"] == "viewer":
                    if bcol1.button("⬆️ Hacer admin", key=f"role_{u['id']}",
                                    use_container_width=True):
                        done, msg = auth.set_role(u["id"], "admin", USER["id"])
                        if done:
                            st.rerun()
                        st.error(msg)
                else:
                    if bcol1.button("⬇️ Quitar admin", key=f"role_{u['id']}",
                                    use_container_width=True):
                        done, msg = auth.set_role(u["id"], "viewer", USER["id"])
                        if done:
                            st.rerun()
                        st.error(msg)
                # Activo / inactivo
                if u["is_active"]:
                    if bcol2.button("⏸️ Desactivar", key=f"act_{u['id']}",
                                    use_container_width=True, disabled=es_yo):
                        done, msg = auth.set_active(u["id"], False, USER["id"])
                        if done:
                            st.rerun()
                        st.error(msg)
                else:
                    if bcol2.button("▶️ Activar", key=f"act_{u['id']}",
                                    use_container_width=True):
                        done, msg = auth.set_active(u["id"], True, USER["id"])
                        if done:
                            st.rerun()
                        st.error(msg)
                # Enlace de restablecimiento
                if bcol3.button("📧 Enviar enlace", key=f"rst_{u['id']}",
                                use_container_width=True,
                                help="Envía un email para que restablezca su contraseña"):
                    row = auth.get_user_by_id(u["id"])
                    sent, send_msg = auth.send_reset_link(row, APP_BASE_URL, welcome=False)
                    if sent:
                        st.success(f"Enlace de restablecimiento enviado a {u['email']}.")
                    else:
                        st.error(f"No se pudo enviar el email: {send_msg}")
                # Eliminar
                if bcol4.button("🗑️ Eliminar", key=f"del_{u['id']}",
                                use_container_width=True, disabled=es_yo):
                    done, msg = auth.delete_user(u["id"], USER["id"])
                    if done:
                        st.rerun()
                    st.error(msg)

st.divider()
st.caption(f"Panel SEO · Aromas de Té · v{__version__} · {RELEASE_DATE} · uso interno")
