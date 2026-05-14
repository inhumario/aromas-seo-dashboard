# Changelog

Todas las versiones notables del dashboard SEO de Aromas.

## [0.3.0] - 2026-05-14

### Añadido
- **Persistencia con PostgreSQL** (servicio `seo-postgres` en EasyPanel proyecto `travelia`).
- Tablas: `daily_metrics` (GSC/GA4 diario), `merchant_snapshots` (estado MC en el tiempo), `audit_events` (hitos manuales).
- **Tab "📈 Histórico"** con gráficos temporales:
  - Evolución del estado Merchant Center (legítimos vs rechazados vs warnings).
  - Clicks/impresiones/CTR/posición diarios de GSC.
  - Sesiones y revenue diarios por dominio en GA4.
  - Tabla de hitos manuales editable.
- **Snapshot automático** al cargar el dashboard (throttled: max 1 cada 20h).
- Botón **"📸 Capturar snapshot AHORA"** en sidebar para forzar.
- Backfill automático de últimos 90 días al primer arranque (GA4 + GSC).

### Cambiado
- Dependencia añadida: `psycopg[binary]==3.2.3`.
- Sidebar muestra edad del último snapshot.

## [0.2.0] - 2026-05-14

### Añadido
- Versionado mostrado en el sidebar.
- Timestamp prominente de "última carga de datos" en cada tab.
- Botón **"🔄 Refrescar"** por cada tab (no solo el global).
- Tab **"📋 Changelog"** con histórico de cambios.
- Indicador visual de "datos del caché" vs "datos frescos".
- Vista filtrada del feed Merchant Center: separa productos legítimos (`api|es|ES`) del resto.
- Plan de acción actualizado reflejando los cambios reales hechos el 2026-05-14.

### Cambiado
- TTL del caché reducido de 1 hora a **15 minutos**.
- Caché por función para refrescar selectivamente sin perder todo.
- KPIs del Resumen ahora incluyen estado actual de Merchant Center.

### Notas operativas
- La latencia real de las fuentes sigue siendo: GSC ~48h, GA4 ~5-15min para reports + realtime instantáneo, Merchant Center casi tiempo real.

## [0.1.0] - 2026-05-13

### Añadido
- Dashboard inicial con Streamlit + Caddy.
- Auth con contraseña + noindex (`X-Robots-Tag` + `/robots.txt Disallow:/`).
- Tabs: Resumen, SEO orgánico, GA4, Google Ads (via GA4), Merchant Center, Plan de acción.
- Conexión en vivo a Google Search Console, Google Analytics 4 y Merchant Center.
- Caché 1h con `@st.cache_data(ttl=3600)`.
- Despliegue en EasyPanel proyecto `travelia` en https://seo.aromasdete.com.
