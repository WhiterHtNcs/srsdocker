ARG SING_BOX_IMAGE=ghcr.io/sagernet/sing-box
FROM ${SING_BOX_IMAGE} AS sing-box

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
COPY --from=sing-box /usr/local/bin/sing-box ./sing-box

RUN chmod +x /entrypoint.sh \
    && chmod +x /app/sing-box \
    && mkdir -p /app/config /app/rules /app/rules-dat/geosite /app/rules-dat/geoip /app/rule-set/srs

EXPOSE 9044

ENTRYPOINT ["/entrypoint.sh"]
