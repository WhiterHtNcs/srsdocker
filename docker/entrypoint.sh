#!/bin/sh
set -eu

mkdir -p /app/mapping/bin /app/mapping/config /app/mapping/config/clash /app/mapping/rules /app/mapping/rules-dat/geosite /app/mapping/rules-dat/geoip /app/mapping/rule-set/srs /app/mapping/rule-set/clash
chmod +x /app/mapping/bin/sing-box 2>/dev/null || true

python /app/app.py --sync-cron
cron

exec python /app/app.py
