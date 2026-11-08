#!/usr/bin/env bash
# =============================================================================
#  Pipeline bootstrap.
#
#  Two things happen before anything else:
#    1. the config template is rendered with the real DOMAIN / scheme
#    2. the WordPress application passwords written by the web container are
#       loaded into the environment
#
#  Then it either runs the daily loop or idles so you can exec in.
# =============================================================================
set -Eeuo pipefail

log() { printf '[pipeline] %s\n' "$*"; }

DOMAIN="${DOMAIN:-example.com}"
SITE_SCHEME="${SITE_SCHEME:-http}"
TEMPLATE="${CONFIG_TEMPLATE:-/app/config/sites.yaml}"
RUNTIME="${SITEFARM_CONFIG:-/app/runtime/sites.yaml}"
CRED_FILE="${WP_CREDENTIALS_FILE:-/wpdata/.sitefarm-credentials.env}"

mkdir -p "$(dirname "$RUNTIME")" /app/data /app/output

if [ ! -f "$TEMPLATE" ]; then
    log "FATAL: config template not found at ${TEMPLATE}"
    log "       mount your config into /app/config/sites.yaml"
    exit 1
fi

cp "$TEMPLATE" "$RUNTIME"
sed -i "s/example\.com/${DOMAIN}/g" "$RUNTIME"
if [ -n "${WP_ADMIN_USER:-}" ]; then
    sed -i "s/user: \"editor\"/user: \"${WP_ADMIN_USER}\"/g" "$RUNTIME"
fi
if [ "$SITE_SCHEME" = "http" ]; then
    sed -i 's|https://|http://|g' "$RUNTIME"
fi
log "config rendered -> ${RUNTIME} (${DOMAIN}, ${SITE_SCHEME})"

# ---------------------------------------------------------- credentials -----
if [ -f "$CRED_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$CRED_FILE"
    set +a
    log "loaded $(grep -c '^WP_APP_PASSWORD_' "$CRED_FILE" 2>/dev/null || echo 0) application password(s)"

    # Also bake them into the rendered config. `docker compose exec pipeline
    # python run.py ...` starts a fresh process that does *not* inherit what
    # this script sourced, so without this step every manual command would fail
    # with "site 'diy' has no WordPress application password configured".
    python - "$RUNTIME" <<'PY'
import os
import sys

import yaml

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    cfg = yaml.safe_load(fh) or {}

patched = 0
for site in cfg.get("sites") or []:
    sid = str(site.get("id", ""))
    pw = os.environ.get(f"WP_APP_PASSWORD_{sid.upper()}")
    if sid and pw:
        site.setdefault("wp", {})["app_password"] = pw
        patched += 1

with open(path, "w", encoding="utf-8") as fh:
    yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)

print(f"[pipeline] wrote {patched} application password(s) into {path}")
PY
else
    log "WARN: ${CRED_FILE} not found -- publishing will fail until the web tier has provisioned the sites"
fi

# ------------------------------------------------------------ wait for web --
if [ "${WAIT_FOR_WEB:-true}" = "true" ]; then
    log "waiting for the web tier ..."
    for _ in $(seq 1 80); do
        if python - <<'PY' 2>/dev/null
import urllib.error
import urllib.request
try:
    urllib.request.urlopen("http://web/", timeout=3)
except urllib.error.HTTPError:
    pass          # any HTTP status means Apache is up; WordPress may still be installing
PY
        then
            log "web tier is responding"
            break
        fi
        sleep 3
    done
fi

# ------------------------------------------------------------------ run -----
if [ "${DAILY_ENABLED:-false}" = "true" ]; then
    log "daily mode: cycle every ${DAILY_INTERVAL:-86400}s, ${DAILY_GEN_COUNT:-3} article(s) per site"
    while true; do
        log "--- cycle start ---"
        python run.py fetch      || log "fetch failed"
        python run.py gen --count "${DAILY_GEN_COUNT:-3}" || log "gen failed"
        python run.py publish --count 60 --wp-status "${PUBLISH_STATUS:-draft}" || log "publish failed"
        python run.py submit     || log "submit failed"
        python run.py status     || true
        log "--- sleeping ---"
        sleep "${DAILY_INTERVAL:-86400}"
    done
else
    log "idle mode. Trigger work with:"
    log "  docker compose exec pipeline python run.py gen --count 2"
    log "  docker compose exec pipeline python run.py publish --wp-status draft"
    exec sleep infinity
fi
