#!/bin/sh
set -eu

mkdir -p /app/rules /app/rules-dat/geosite /app/rules-dat/geoip /app/rule-set/srs

CRON_FILE=/etc/cron.d/singbox-srs-generator

python - <<'PY' > "$CRON_FILE"
import json
import os
import re
from pathlib import Path

config_path = Path(os.environ.get("CONFIG_PATH", "/app/config/config.json"))
config = {}
if config_path.exists():
    config = json.loads(config_path.read_text(encoding="utf-8"))

enabled = bool(config.get("auto_update_enabled", False))
schedule = str(config.get("auto_update_cron", "0 4 * * *")).strip() or "0 4 * * *"
fields = schedule.split()
valid = len(fields) == 5 and all(re.fullmatch(r"[A-Za-z0-9*/,\-]+", field) for field in fields)
if not valid:
    enabled = False
    schedule = "0 4 * * *"

for key in ("GEOSITE_URL", "GEOIP_URL", "GITHUB_TOKEN"):
    value = os.environ.get(key)
    if value and "\n" not in value and "\r" not in value:
        print(f'{key}="{value}"')

if enabled:
    print(
        f"{schedule} root cd /app && /usr/local/bin/python /app/app.py "
        "--update-remote-rules >> /proc/1/fd/1 2>> /proc/1/fd/2"
    )
else:
    print("# singbox-srs-generator remote rule auto update is disabled")
PY

chmod 0644 "$CRON_FILE"
cron

exec python /app/app.py
