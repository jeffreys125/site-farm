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

# Global config: ServerName (silences AH00558, keeps self-referential redirects
# on the public hostname) plus the per-directory rules every site needs.
#
# The rewrite rules live here rather than in each site's .htaccess on purpose:
# wp-cli runs without a web server context, so `wp rewrite flush --hard` refuses
# to write .htaccess ("Regenerating a .htaccess file requires special
# configuration") and every pretty permalink -- including /wp-json/ -- 404s
# until someone opens wp-admin and clicks Save. Doing it at the vhost level also
# survives a plugin or an update wiping the file.
cat > /etc/apache2/conf-available/sitefarm.conf <<CONF
ServerName ${DOMAIN}

<Directory /var/www/sites>
    Options FollowSymLinks
    AllowOverride All
    Require all granted

    RewriteEngine On
    RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]
    RewriteBase /
    RewriteRule ^index\\.php\$ - [L]
    RewriteCond %{REQUEST_FILENAME} !-f
    RewriteCond %{REQUEST_FILENAME} !-d
    RewriteRule . /index.php [L]
</Directory>
CONF
a2enconf sitefarm >/dev/null 2>&1 || true

mkdir -p "$WP_ROOT"
chown -R www-data:www-data "$WP_ROOT"

if [ -z "$WP_ADMIN_PASSWORD" ]; then
    WP_ADMIN_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)"
    log "generated admin password: ${WP_ADMIN_PASSWORD}   <-- save this, it is not shown again"
fi

# Say out loud which account the sites get. 'editor' is the compose default, so
# seeing it means WP_ADMIN_USER never reached the container -- otherwise people
# spend hours wondering why logging in with the name from .env says "no such
# user" while the reason is a typo'd or commented-out line.
if [ "$WP_ADMIN_USER" = "editor" ]; then
    log "admin account: editor <${WP_ADMIN_EMAIL}>  (built-in default)"
    log "               WP_ADMIN_USER is not set in .env -- edit it there, then"
    log "               'docker compose up -d web'. Existing sites keep their old"
    log "               account, so this only renames/creates on the next boot."
else
    log "admin account: ${WP_ADMIN_USER} <${WP_ADMIN_EMAIL}>"
fi

# ------------------------------------------------------------------ db ------
# Readiness is probed with PHP's mysqli, not the mysql CLI. Two reasons:
#   - the CLI shipped by default-mysql-client is MariaDB's, which refuses
#     MySQL 8's self-signed TLS certificate ("TLS/SSL error" on every ping);
#   - mysqli is exactly the stack WordPress itself uses, so a green probe here
#     means WordPress will connect too.
log "waiting for MySQL at ${DB_HOST} ..."
db_ready=0
for _ in $(seq 1 90); do
    if php -r 'exit(@mysqli_connect(getenv("DB_HOST"), getenv("DB_USER"), getenv("DB_PASSWORD")) ? 0 : 1);' >/dev/null 2>&1; then
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
        "https://cdn.jsdelivr.net/gh/wp-cli/builds@gh-pages/phar/wp-cli.phar" \
        "https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar" \
        "https://github.com/wp-cli/wp-cli/releases/latest/download/wp-cli.phar" \
    ; do
        if curl -fsSL --http1.1 --connect-timeout 15 --max-time 300 \
                --speed-limit 4096 --speed-time 30 --retry 2 --retry-delay 2 --retry-all-errors \
                -o /usr/local/bin/wp "$url" 2>/dev/null; then
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

# --------------------------------------------------------------- helpers ----
# wp-config.php pins WP_HOME / WP_SITEURL as constants, and WordPress ignores
# the database rows once those constants exist. So changing DOMAIN or
# SITE_SCHEME in .env would leave every existing site on the old address --
# `up -d` recreates the container but the files on the volume stay as they were.
# Rewrite the two lines on every boot; it is a no-op when nothing moved.
sync_site_url() {
    local id="$1" url="$2"
    local dir="${WP_ROOT}/${id}"
    local cfg="${dir}/wp-config.php"

    grep -q "define('WP_HOME', '${url}');" "$cfg" 2>/dev/null && return 0

    log "[${id}] address changed, rewriting wp-config -> ${url}"
    sed -i "s|define('WP_HOME', '.*');|define('WP_HOME', '${url}');|" "$cfg"
    sed -i "s|define('WP_SITEURL', '.*');|define('WP_SITEURL', '${url}');|" "$cfg"

    # Keep the database in step too: plugins and some themes read the raw
    # options instead of the constants.
    wp option update home "$url" --allow-root --path="$dir" --quiet >/dev/null 2>&1 || true
    wp option update siteurl "$url" --allow-root --path="$dir" --quiet >/dev/null 2>&1 || true
}

# wp core install only ever runs once, on the first boot of a site. Changing
# WP_ADMIN_USER in .env afterwards therefore had no effect at all -- the site
# kept its original login, and the only feedback was "already installed,
# skipping" while wp-login.php kept rejecting the new name. Add the requested
# account if it is missing. Existing accounts are never modified: their
# password may have been changed in wp-admin since, and silently resetting it
# on every restart would be worse than the original problem.
ensure_admin_user() {
    local id="$1" dir="$2"

    if wp user get "${WP_ADMIN_USER}" --allow-root --path="$dir" --field=ID >/dev/null 2>&1; then
        return 0
    fi

    local out
    if out=$(wp user create "${WP_ADMIN_USER}" "${WP_ADMIN_EMAIL}" \
                --role=administrator --user_pass="${WP_ADMIN_PASSWORD}" \
                --allow-root --path="$dir" 2>&1); then
        log "[${id}] created administrator: ${WP_ADMIN_USER} / ${WP_ADMIN_PASSWORD}"
        return 0
    fi

    # Most common failure: the address is already taken by the account created
    # on first boot (people set WP_ADMIN_USER after installing, but keep the
    # same email). If that owner is an administrator, rename it instead of
    # creating a duplicate -- this keeps post authorship and settings intact.
    if printf '%s' "$out" | grep -q "email address is already used"; then
        local owner roles
        owner=$(wp user get "${WP_ADMIN_EMAIL}" --allow-root --path="$dir" --field=ID 2>/dev/null || true)
        roles=$(wp user get "$owner" --allow-root --path="$dir" --field=roles 2>/dev/null || true)
        if [ -n "$owner" ] && printf '%s' "$roles" | grep -q administrator; then
            wp user update "$owner" --user_pass="${WP_ADMIN_PASSWORD}" \
                --user_email="${WP_ADMIN_EMAIL}" --allow-root --path="$dir" --quiet >/dev/null 2>&1 || true
            # wp-cli refuses to change user_login ("User logins can't be
            # changed") and wp eval does not take positional args, so the login
            # is inlined. Quotes are stripped: a WordPress login can never
            # contain one, and the value comes from our own .env.
            local login
            login=$(printf '%s' "${WP_ADMIN_USER}" | tr -d "'")
            wp eval "
                global \$wpdb;
                \$wpdb->update(
                    \$wpdb->users,
                    array('user_login' => '${login}', 'user_nicename' => sanitize_title('${login}')),
                    array('ID' => ${owner})
                );
            " --allow-root --path="$dir" >/dev/null 2>&1 || true

            if wp user get "${WP_ADMIN_USER}" --allow-root --path="$dir" --field=ID >/dev/null 2>&1; then
                log "[${id}] renamed administrator #${owner} to '${WP_ADMIN_USER}' (that email was already in use)"
                return 0
            fi
            log "[${id}] WARN: found existing administrator #${owner} but renaming it failed"
            return 1
        fi
    fi

    log "[${id}] WARN: could not create administrator '${WP_ADMIN_USER}': ${out}"
}

# Creates a database with PHP's mysqli. Three ways to do this all fail:
#   - the mysql CLI: MariaDB client rejects MySQL 8's self-signed TLS cert
#   - wp db query:   it connects *to* the database it is about to create
#   - mysql -e "...": same TLS problem
# so plain mysqli, which is also what WordPress uses.
create_database() {
    php -r '
        mysqli_report(MYSQLI_REPORT_OFF);
        $c = @mysqli_connect(getenv("DB_HOST"), getenv("DB_USER"), getenv("DB_PASSWORD"));
        if (!$c) {
            fwrite(STDERR, "cannot reach MySQL\n");
            exit(1);
        }
        $db = str_replace("`", "", $argv[1]);
        $sql = "CREATE DATABASE IF NOT EXISTS `{$db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci";
        if (!@mysqli_query($c, $sql)) {
            fwrite(STDERR, mysqli_error($c) . "\n");
            exit(2);
        }
    ' "$1"
}

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
        sync_site_url "$id" "$url"
        # must not abort the boot: a missing account is reported, not fatal
        ensure_admin_user "$id" "$dir" || true
        log "[${id}] already installed, skipping"
        return 0
    fi

    log "[${id}] provisioning -> ${url}"
    mkdir -p "$dir"
    cp -a "${CORE}/." "$dir/"

    if ! create_database "$dbname"; then
        log "[${id}] FAILED: could not create database ${dbname}"
        return 1
    fi

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

    # Application Passwords are refused by WordPress whenever is_ssl() is false,
    # which turns every pipeline REST call into 401 rest_not_logged_in even with
    # correct credentials. A must-use plugin re-enables them; wp-config cannot
    # do it because add_filter() does not exist yet at that point.
    mkdir -p "$dir/wp-content/mu-plugins"
    cat > "$dir/wp-content/mu-plugins/sitefarm-app-passwords.php" <<'MU'
<?php
/**
 * Plugin Name: Sitefarm - Application Passwords over plain HTTP
 * Description: Allows REST authentication through WordPress Application
 *              Passwords when TLS terminates outside the container.
 */
add_filter( 'wp_is_application_passwords_available', '__return_true' );
MU

    if ! wp core install --allow-root --path="$dir" \
        --url="$url" --title="$title" \
        --admin_user="${WP_ADMIN_USER}" --admin_password="${WP_ADMIN_PASSWORD}" \
        --admin_email="${WP_ADMIN_EMAIL}" --skip-email --quiet; then
        log "[${id}] FAILED: wp core install returned an error"
        return 1
    fi

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

# Readiness marker for the pipeline container. Removed here and re-created once
# every site is installed, so the pipeline can tell "still provisioning" from
# "done". Without it the pipeline only sees a non-empty credentials file and
# carries on after the *first* site -- the remaining eight passwords are not
# written yet, and publishing silently fails for all of them.
READY_FILE="${WP_ROOT}/.sitefarm-ready"
rm -f "$READY_FILE"

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
touch "$READY_FILE"
log "starting Apache ..."
exec "$@"
