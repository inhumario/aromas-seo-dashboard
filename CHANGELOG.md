# Changelog

Todas las versiones notables del dashboard SEO de Aromas.

## [0.5.0] - 2026-05-15

### Añadido
- **Acceso multi-usuario con usuario y contraseña** (antes una única contraseña global):
  - Tabla `users` en Postgres con contraseñas cifradas (PBKDF2-SHA256 + sal, 240k iteraciones).
  - Login por usuario **o** email, indistintamente.
  - Roles: **Administrador** (gestiona usuarios) y **Visualización** (solo consulta).
- **Tab "👤 Cuenta"**:
  - "Mi perfil" y cambio de contraseña propia para cualquier usuario.
  - "Gestión de usuarios" (solo admin): crear usuarios, cambiar rol, activar/desactivar,
    eliminar y enviar enlaces de restablecimiento.
- **Recuperación de contraseña por email**: enlace de un solo uso con caducidad de 60 min,
  enviado vía Gmail SMTP. Accesible desde la pantalla de acceso ("He olvidado mi contraseña").
- **Email de bienvenida**: al crear un usuario se le puede enviar un enlace para que
  configure él mismo su contraseña.
- **Cambio de contraseña obligatorio** en el primer acceso de cada cuenta nueva.
- Pantalla de acceso rediseñada, centrada y con tema de marca (terracota / crema).
- Acceso de emergencia con la contraseña general si Postgres no está disponible.

### Cambiado
- La variable `DASHBOARD_PASSWORD` deja de ser el acceso real: solo siembra el primer
  administrador y sirve de acceso de emergencia. Tras el primer login, las credenciales
  viven en la base de datos.
- Tema de marca aplicado vía `.streamlit/config.toml`.
- Se oculta el cromo por defecto de Streamlit (menú, toolbar, footer "Made with Streamlit").
- Eliminados los enlaces a GitHub del sidebar y del pie de página.

### Notas operativas
- ENV opcionales nuevas: `ADMIN_USERNAME`, `ADMIN_EMAIL`, `ADMIN_FULL_NAME`, `APP_BASE_URL`.
- La recuperación de contraseña reutiliza `GMAIL_USER` / `GMAIL_APP_PASSWORD` (ya presentes
  desde v0.4.0 para las alertas).

## [0.4.0] - 2026-05-14

### Añadido
- **Cron daemon** dentro del contenedor — `snapshot.py` se ejecuta cada noche a las **03:00 Europe/Madrid**, sin necesidad de abrir el dashboard.
- **Sistema de alertas configurables**:
  - Tab "🔔 Alertas" con UI para CRUD de reglas.
  - 11 métricas soportadas: clicks/impresiones/posición/CTR GSC (.com y .eu), sesiones/revenue/transacciones GA4, productos rechazados/warnings Merchant.
  - 4 condiciones: `lt`, `gt`, `pct_drop_vs_avg`, `pct_rise_vs_avg`.
  - 2 ventanas de comparación: media de últimos 7 días o 30 días.
  - Emails a uno o varios destinatarios cuando la alerta se dispara.
  - Botón "Evaluar AHORA" para probar sin esperar al cron.
  - Histórico de eventos disparados visible en la tab.
- **Script `snapshot.py` standalone** invocable desde CLI: `python snapshot.py [--skip-alerts]`.

### Cambiado
- Dockerfile incluye `cron` y crontab `/etc/cron.d/seo-cron`.
- `entrypoint.sh` arranca `cron` antes que Streamlit/Caddy.
- ENV adicionales: `GMAIL_USER`, `GMAIL_APP_PASSWORD` para envío de alertas.

### Notas operativas
- El cron diario captura snapshot + evalúa alertas. Si una se dispara, se envía email vía Gmail SMTP.
- Las credenciales SMTP son las de `cuadrado.mario@aromasdete.com` (mismo `gmail_smtp.env` del host).

## [0.3.0] - 2026-05-14

### Añadido
- **Persistencia con PostgreSQL** (servicio `seo-postgres` en EasyPanel proyecto `travelia`).
- Tablas: `daily_metrics` (GSC/GA4 diario), `merchant_snapshots` (estado MC en el tiempo), `audit_events` (hitos manuales).
- **Tab "📈 Histórico"** con gráficos temporales (Merchant, GSC diario, GA4 diario por dominio).
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

## [0.1.0] - 2026-05-13

### Añadido
- Dashboard inicial con Streamlit + Caddy.
- Auth con contraseña + noindex (`X-Robots-Tag` + `/robots.txt Disallow:/`).
- Tabs: Resumen, SEO orgánico, GA4, Google Ads (via GA4), Merchant Center, Plan de acción.
- Conexión en vivo a Google Search Console, Google Analytics 4 y Merchant Center.
- Caché 1h con `@st.cache_data(ttl=3600)`.
- Despliegue en EasyPanel proyecto `travelia` en https://seo.aromasdete.com.
