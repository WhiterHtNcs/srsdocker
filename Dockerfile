FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

COPY app.py ./
COPY config ./config
COPY web ./web
COPY rules ./rules
COPY rules-dat ./rules-dat
COPY rule-set ./rule-set
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh \
    && mkdir -p /app/bin /app/config /app/rules /app/rules-dat/geosite /app/rules-dat/geoip /app/rule-set/srs

EXPOSE 9044

ENTRYPOINT ["/entrypoint.sh"]
