FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# Caddy binary + curl + cron
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates cron && \
    curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v2.8.4/caddy_2.8.4_linux_amd64.tar.gz" \
        | tar -xz -C /usr/local/bin caddy && \
    chmod +x /usr/local/bin/caddy && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py /app/app.py
COPY db.py /app/db.py
COPY auth.py /app/auth.py
COPY alerts.py /app/alerts.py
COPY snapshot.py /app/snapshot.py
COPY version.py /app/version.py
COPY CHANGELOG.md /app/CHANGELOG.md
COPY .streamlit/config.toml /app/.streamlit/config.toml
COPY Caddyfile /etc/caddy/Caddyfile
COPY entrypoint.sh /entrypoint.sh
COPY crontab.seo /etc/cron.d/seo-cron

RUN chmod +x /entrypoint.sh && \
    chmod 0644 /etc/cron.d/seo-cron && \
    touch /var/log/seo-snapshot.log && \
    chmod 0644 /var/log/seo-snapshot.log

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:80/_stcore/health || exit 1

CMD ["/entrypoint.sh"]
