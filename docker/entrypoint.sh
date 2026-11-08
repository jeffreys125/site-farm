#!/usr/bin/env bash
# =============================================================================
#  First-boot provisioning for the web tier.
#
#  Idempotent: on every start it re-renders the vhost and checks each site.
#  A site whose wp-config.php already exists is left completely alone, so
#  restarting the container never touches live content.
# =============================================================================
set -Eeuo pipefail

log() { printf '[sitefarm] %s\n' "$*"; }

DOMAIN="${DOMAIN:-example.com}"
SITES="${SITES:-hub,diy,woodworking,pet,camp,kitchen,auto,garden,homeoffice}"
SITE_SCHEME="${SITE_SCHEME:-http}"
BRAND="${BRAND:-Northline}"

WP_ADMIN_USER="${WP_ADMIN_USER:-editor}"
WP_ADMIN_PASSWORD="${WP_ADMIN_PASSWORD:-}"
WP_ADMIN_EMAIL="${WP_ADMIN_EMAIL:-editor@${DOMAIN}}"

DB_HOST="${DB_HOST:-db}"
DB_USER="${DB_USER:-wp}"
DB_PASSWORD="${DB_PASSWORD:-wp}"

WP_ROOT=/var/www/sites
CORE=/opt/wp-core

# ---------------------------------------------------------------- vhost -----
log "rendering vhost for ${DOMAIN} (scheme: ${SITE_SCHEME})"
mkdir -p /etc/apache2/sites-available
sed "s/@DOMAIN@/${DOMAIN}/g" \
    /etc/apache2/sitefarm/wildcard.conf.tpl \
    > /etc/apache2/sites-available/000-wildcard.conf
a2ensite 000-wildcard >/dev/null 2>&1 || true

mkdir -p "$WP_ROOT"
chown -R www-data:www-data "$WP_ROOT"

if [ -z "$WP_ADMIN_PASSWORD" ]; then
    WP_ADMIN_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)"
    log "generated admin password: ${WP_ADMIN_PASSWORD}   <-- save this, it is not shown again"
fi

# ------------------------------------------------------------------ db ------
log "waiting for MySQL at ${DB_HOST} ..."
db_ready=0
for _ in $(seq 1 90); do
    if mysqladmin ping -h"${DB_HOST}" -u"${DB_USER}" -p"${DB_PASSWORD}" --silent >/dev/null 2>&1; then
        db_ready=1
        break
    fi
    sleep 2
done
if [ "$db_ready" -ne 1 ]; then
    log "FATAL: MySQL never became reachable -- check DB_HOST / DB_PASSWORD"
    exit 1
fi
log "MySQL is up"

# ------------------------------------------------------------- wp-cli ------
# The image tries to bake wp-cli in at build time. If that failed (blocked
# network, mirror trouble) this is the second and last attempt.
ensure_wp_cli() {
    if wp --allow-root --version >/dev/null 2>&1; then
        return 0
    fi
    log "wp-cli missing, fetching ..."
    for url in \
        "https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar" \
        "https://github.com/wp-cli/wp-cli/releases/latest/download/wp-cli.phar" \
        "https://raw.gitmirror.com/wp-cli/builds/gh-pages/phar/wp-cli.phar" \
    ; do
        if curl -fsSL --http1.1 --retry 4 --retry-delay 3 --retry-all-errors \
                --connect-timeout 20 --max-time 180 -o /usr/local/bin/wp "$url" 2>/dev/null; then
            chmod +x /usr/local/bin/wp
            if wp --allow-root --version >/dev/null 2>&1; then
                log "wp-cli ready"
                return 0
            fi
        fi
        log "  failed: ${url}"
    done
    log "FATAL: cannot install wp-cli -- no route to any mirror"
    log "       fix the container's outbound network, or copy wp-cli.phar to"
    log "       /usr/local/bin/wp manually and restart"
    return 1
}

ensure_wp_cli || exit 1

# ---------------------------------------------------------------- core ------
if [ ! -f "${CORE}/wp-load.php" ]; then
    log "WordPress core missing, downloading ..."
    rm -rf "${CORE:?}"/* 2>/dev/null || true
    curl -fsSL --http1.1 --retry 5 --retry-delay 3 --retry-all-errors \
        --connect-timeout 20 --max-time 300 https://wordpress.org/latest.tar.gz \
        | tar -xz -C "$CORE" --strip-components=1
fi

# --------------------------------------------------------------- sites ------
install_site() {
    local id="$1" title="$2"
    local dir="${WP_ROOT}/${id}"
    local dbname="wp_${id}"
    local host="${id}.${DOMAIN}"
    local url

    if [ "$id" = "hub" ]; then
        url="${SITE_SCHEME}://${DOMAIN}"
    else
        url="${SITE_SCHEME}://${host}"
    fi

    if [ -f "${dir}/wp-config.php" ]; then
        log "[${id}] already installed, skipping"
        return 0
    fi

    log "[${id}] provisioning -> ${url}"
    mkdir -p "$dir"
    cp -a "${CORE}/." "$dir/"

    mysql -h"${DB_HOST}" -u"${DB_USER}" -p"${DB_PASSWORD}" \
        -e "CREATE DATABASE IF NOT EXISTS \`${dbname}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" \
        2>/dev/null || true

    wp config create --allow-root --path="$dir" \
        --dbname="$dbname" --dbuser="${DB_USER}" --dbpass="${DB_PASSWORD}" --dbhost="${DB_HOST}" \
        --skip-check --quiet \
        --extra-php <<PHP
define('WP_HOME', '${url}');
define('WP_SITEURL', '${url}');
define('WP_MEMORY_LIMIT', '256M');
define('DISALLOW_FILE_EDIT', true);
define('AUTOMATIC_UPDATER_DISABLED', false);
define('WP_POST_REVISIONS', 5);

// Behind a TLS-terminating reverse proxy (Caddy, Nginx, BaoTa) the container
// only ever sees plain HTTP. Without this, is_ssl() returns false and you get
// redirect loops plus mixed-content warnings.
if (isset(\$_SERVER['HTTP_X_FORWARDED_PROTO']) && \$_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') {
    \$_SERVER['HTTPS'] = 'on';
}
if (isset(\$_SERVER['HTTP_X_FORWARDED_HOST'])) {
    \$_SERVER['HTTP_HOST'] = \$_SERVER['HTTP_X_FORWARDED_HOST'];
}
PHP

    wp core install --allow-root --path="$dir" \
        --url="$url" --title="$title" \
        --admin_user="${WP_ADMIN_USER}" --admin_password="${WP_ADMIN_PASSWORD}" \
        --admin_email="${WP_ADMIN_EMAIL}" --skip-email --quiet

    # sensible defaults for a content site
    wp rewrite structure '/%postname%/' --allow-root --path="$dir" --quiet
    wp option update default_comment_status closed --allow-root --path="$dir" --quiet
    wp option update default_ping_status closed --allow-root --path="$dir" --quiet
    wp option update blogdescription "" --allow-root --path="$dir" --quiet
    wp option update timezone_string "UTC" --allow-root --path="$dir" --quiet

    # drop the demo content that ships with a fresh install
    wp post delete 1 2 3 --force --allow-root --path="$dir" >/dev/null 2>&1 || true
    wp plugin delete akismet hello --allow-root --path="$dir" >/dev/null 2>&1 || true

    chown -R www-data:www-data "$dir"
    chmod 640 "${dir}/wp-config.php"
    log "[${id}] done"
}

# ------------------------------------------------------------ credentials ---
# The pipeline authenticates over the REST API, which needs an Application
# Password per site. Generating them here means you never have to open eight
# wp-admin dashboards by hand.
CRED_FILE="${WP_ROOT}/.sitefarm-credentials.env"
touch "$CRED_FILE"

ensure_app_password() {
    local id="$1"
    local dir="${WP_ROOT}/${id}"
    local key="WP_APP_PASSWORD_$(echo "$id" | tr '[:lower:]' '[:upper:]')"

    if grep -q "^${key}=" "$CRED_FILE" 2>/dev/null; then
        return 0
    fi
    [ -f "${dir}/wp-config.php" ] || return 0

    local pw
    pw="$(wp user application-password create "${WP_ADMIN_USER}" "content-pipeline" \
            --porcelain --allow-root --path="$dir" 2>/dev/null || true)"
    if [ -n "$pw" ]; then
        printf '%s=%s\n' "$key" "$pw" >> "$CRED_FILE"
        log "[${id}] application password created"
    else
        log "[${id}] WARNING: could not create application password"
    fi
}

IFS=',' read -ra ENTRIES <<< "$SITES"
for entry in "${ENTRIES[@]}"; do
    entry="$(echo "$entry" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    [ -z "$entry" ] && continue
    id="${entry%%:*}"
    title="${entry#*:}"
    [ "$title" = "$id" ] && title="${BRAND} $(echo "${id:0:1}" | tr '[:lower:]' '[:upper:]')${id:1}"

    id="$(echo "$id" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
    [ -z "$id" ] && continue

    if ! install_site "$id" "$title"; then
        log "[${id}] INSTALL FAILED -- container will still start so you can debug"
    fi
    ensure_app_password "$id" || true
done

chmod 600 "$CRED_FILE" 2>/dev/null || true
chown www-data:www-data "$CRED_FILE" 2>/dev/null || true

log "sites ready: $(ls -1 "$WP_ROOT" 2>/dev/null | grep -v '^\.' | tr '\n' ' ')"
log "credentials: ${CRED_FILE} ($(grep -c '^WP_APP_PASSWORD_' "$CRED_FILE" 2>/dev/null || echo 0) site(s))"
log "starting Apache ..."
exec "$@"
