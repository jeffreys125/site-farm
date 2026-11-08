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

# wp-cli -- used once at first boot to install each site non-interactively
RUN curl -fsSL -o /usr/local/bin/wp \
        https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar \
    && chmod +x /usr/local/bin/wp \
    && wp --allow-root --version

# WordPress core is fetched at build time so first boot does not depend on
# wordpress.org being reachable. entrypoint.sh re-downloads if this step failed.
RUN mkdir -p /opt/wp-core \
    && (curl -fsSL https://wordpress.org/latest.tar.gz | tar -xz -C /opt/wp-core --strip-components=1) \
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
