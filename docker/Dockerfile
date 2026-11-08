# =============================================================================
#  Web tier: one Apache container serving N independent WordPress installs,
#  routed by subdomain through mod_vhost_alias.
#
#  Why one container instead of N:
#    Eight separate WordPress containers would need eight PHP-FPM pools and
#    eight sets of Apache overhead -- roughly 3.5 GB of RAM for the same work
#    this container does in ~600 MB. Isolation that matters here is at the
#    database and filesystem level, and both are separate per site.
# =============================================================================
FROM php:8.2-apache

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpng-dev libjpeg62-turbo-dev libfreetype6-dev libzip-dev \
        libonig-dev libxml2-dev libicu-dev \
        default-mysql-client curl unzip ca-certificates less \
    && docker-php-ext-configure gd --with-freetype --with-jpeg \
    && docker-php-ext-install -j"$(nproc)" gd mysqli pdo_mysql zip mbstring exif intl opcache \
    && a2enmod rewrite headers expires deflate vhost_alias ssl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# wp-cli -- used once at first boot to install each site non-interactively.
#
# Source order matters: jsDelivr mirrors GitHub and is fast (and reachable)
# nearly everywhere, while raw.githubusercontent.com is frequently blocked or
# throttled to a few kB/s. Measured here: 7 MB in 3 s from jsDelivr versus
# 1.9 MB in 60 s from raw.github.
#
# If every source fails the build still succeeds -- entrypoint.sh retries at
# boot with the same list.
RUN for url in \
        "https://cdn.jsdelivr.net/gh/wp-cli/builds@gh-pages/phar/wp-cli.phar" \
        "https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar" \
        "https://github.com/wp-cli/wp-cli/releases/latest/download/wp-cli.phar" \
    ; do \
        echo "--- trying $url"; \
        if curl -fsSL --http1.1 --connect-timeout 15 --max-time 300 \
                --speed-limit 4096 --speed-time 30 --retry 2 --retry-delay 2 --retry-all-errors \
                -o /usr/local/bin/wp "$url"; then \
            chmod +x /usr/local/bin/wp; \
            if wp --allow-root --version; then break; fi; \
        fi; \
        echo "WARN: wp-cli fetch failed from $url"; \
        rm -f /usr/local/bin/wp; \
    done; \
    test -x /usr/local/bin/wp && wp --allow-root --version \
        || echo "WARN: wp-cli unavailable at build time, entrypoint.sh will retry at first boot"

# WordPress core is fetched at build time so first boot does not depend on
# wordpress.org being reachable. entrypoint.sh re-downloads if this step failed.
RUN mkdir -p /opt/wp-core \
    && (curl -fsSL --http1.1 --connect-timeout 15 --max-time 300 \
            --speed-limit 4096 --speed-time 30 --retry 3 --retry-delay 3 --retry-all-errors \
            https://wordpress.org/latest.tar.gz \
        | tar -xz -C /opt/wp-core --strip-components=1) \
    || echo "WARN: could not pre-fetch WordPress core; will retry at first boot"

COPY php.ini /usr/local/etc/php/conf.d/zz-sitefarm.ini
COPY apache-wildcard.conf.tpl /etc/apache2/sitefarm/wildcard.conf.tpl
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && a2dissite 000-default.conf >/dev/null 2>&1 || true

WORKDIR /var/www/sites
EXPOSE 80

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["apache2-foreground"]
