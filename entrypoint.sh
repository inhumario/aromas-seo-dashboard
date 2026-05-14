#!/bin/bash
set -e

# Exportar las env vars que necesita el cron (el daemon de cron no hereda environment del padre)
echo "GOOGLE_TOKEN_B64=${GOOGLE_TOKEN_B64}" >> /etc/environment
echo "DATABASE_URL=${DATABASE_URL}" >> /etc/environment
echo "GMAIL_USER=${GMAIL_USER:-}" >> /etc/environment
echo "GMAIL_APP_PASSWORD=${GMAIL_APP_PASSWORD:-}" >> /etc/environment
echo "TZ=${TZ:-Europe/Madrid}" >> /etc/environment

# Configurar TZ
ln -snf "/usr/share/zoneinfo/${TZ:-Europe/Madrid}" /etc/localtime || true

# Activar cron (el daemon corre en background)
cron

# Arrancar Streamlit en background
streamlit run /app/app.py \
    --server.port=8501 \
    --server.address=127.0.0.1 \
    --server.headless=true \
    --browser.gatherUsageStats=false \
    --server.enableCORS=false \
    --server.enableXsrfProtection=true &

# Esperar Streamlit
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8501/_stcore/health >/dev/null 2>&1; then
        echo "Streamlit ready after ${i}s"
        break
    fi
    sleep 1
done

# Caddy en foreground
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
