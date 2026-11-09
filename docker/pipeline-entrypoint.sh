#!/usr/bin/env bash
# =============================================================================
#  Pipeline bootstrap.
#
#  Order matters:
#    1. the WordPress application passwords written by the web container are
#       loaded into the environment
#    2. the config template is rendered with the real DOMAIN / scheme, with
#       those passwords baked in
#    3. either start the daily loop, or idle so you can exec in
#
#  Credentials come first because step 2 writes them into the file.
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

# ---------------------------------------------------------- credentials -----
if [ -f "$CRED_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$CRED_FILE"
    set +a
    log "loaded $(grep -c '^WP_APP_PASSWORD_' "$CRED_FILE" 2>/dev/null || echo 0) application password(s)"
else
    log "WARN: ${CRED_FILE} not found -- publishing will fail until the web tier has provisioned the sites"
fi

# --------------------------------------------------------------- render -----
cp "$TEMPLATE" "$RUNTIME"

# This used to be three `sed` calls, which quietly rewrote every https:// in the
# file -- including project.deepseek.base_url, turning the endpoint into
# http://api.deepseek.com and breaking generation whenever SITE_SCHEME=http
# (the default). Walking the YAML touches only the fields that describe a site.
#
# The passwords are baked in as well because `docker compose exec pipeline
# python run.py ...` starts a fresh process that does not inherit what this
# script sourced; without it, every manual command fails with "site 'diy' has
# no WordPress application password configured".
python - "$RUNTIME" <<'PY'
import os
import re
import sys

import yaml

path = sys.argv[1]
domain = os.environ.get("DOMAIN", "example.com")
scheme = os.environ.get("SITE_SCHEME", "http")
admin = os.environ.get("WP_ADMIN_USER", "")

with open(path, encoding="utf-8") as fh:
    cfg = yaml.safe_load(fh) or {}


def walk(node, keys=()):
    if isinstance(node, dict):
        return {k: walk(v, keys + (k,)) for k, v in node.items()}
    if isinstance(node, list):
        return [walk(v, keys) for v in node]
    # leave the model endpoint alone: it is not our domain and must keep https
    if keys[:2] == ("project", "deepseek"):
        return node
    return node.replace("example.com", domain) if isinstance(node, str) else node


cfg = walk(cfg)

passworded = 0
for site in cfg.get("sites") or []:
    if isinstance(site.get("base_url"), str):
        site["base_url"] = re.sub(r"^https?://", f"{scheme}://", site["base_url"])
    wp = site.setdefault("wp", {})
    if admin:
        wp["user"] = admin
    sid = str(site.get("id", ""))
    password = os.environ.get(f"WP_APP_PASSWORD_{sid.upper()}")
    if sid and password:
        wp["app_password"] = password
        passworded += 1

with open(path, "w", encoding="utf-8") as fh:
    yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)

print(f"[pipeline] rendered {path}: {domain} / {scheme} / {passworded} password(s)")
PY

log "config rendered -> ${RUNTIME} (${DOMAIN}, ${SITE_SCHEME})"

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
