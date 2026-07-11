FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

COPY app.py ./
COPY web ./web
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh \
    && mkdir -p /app/mapping/bin /app/mapping/config /app/mapping/rules /app/mapping/rules-dat/geosite /app/mapping/rules-dat/geoip /app/mapping/rule-set/srs /app/mapping/rule-set/openclash /app/mapping/rule-set/openclash/providers

EXPOSE 9044

ENTRYPOINT ["/entrypoint.sh"]
