#!/bin/bash
set -e

# Arrancar Streamlit en background
streamlit run /app/app.py \
    --server.port=8501 \
    --server.address=127.0.0.1 \
    --server.headless=true \
    --browser.gatherUsageStats=false \
    --server.enableCORS=false \
    --server.enableXsrfProtection=true &

# Esperar a que Streamlit esté listo (max 30s)
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8501/_stcore/health >/dev/null 2>&1; then
        echo "Streamlit ready after ${i}s"
        break
    fi
    sleep 1
done

# Lanzar Caddy en foreground
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
