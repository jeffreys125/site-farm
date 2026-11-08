# =============================================================================
#  Rendered by entrypoint.sh at container start -- do not edit this template's
#  @DOMAIN@ placeholder unless you also change it there.
#
#  Routing model:
#   example.com        -> /var/www/sites/hub          (brand site, optional)
#   *.example.com      -> /var/www/sites/<subdomain>  (one WordPress per sub)
# =============================================================================

# --- brand / hub site -------------------------------------------------------
<VirtualHost *:80>
    ServerName  @DOMAIN@
    ServerAlias www.@DOMAIN@
    DocumentRoot /var/www/sites/hub

    # WordPress Application Passwords send Basic auth; Apache drops the header
    # unless it is explicitly re-exported here.
    SetEnvIf Authorization "(.*)" HTTP_AUTHORIZATION=$1

    ErrorLog  /proc/self/fd/2
    CustomLog /proc/self/fd/1 combined
</VirtualHost>

# --- subdomain sites --------------------------------------------------------
# %1 expands to the first label of the Host header, so diy.example.com maps to
# /var/www/sites/diy with no per-site vhost and no restart when you add one.
<VirtualHost *:80>
    ServerAlias *.@DOMAIN@
    VirtualDocumentRoot /var/www/sites/%1

    SetEnvIf Authorization "(.*)" HTTP_AUTHORIZATION=$1

    # Do not let the network leak into search results as one site.
    Header always set X-Content-Type-Options "nosniff"
    Header always set Referrer-Policy "strict-origin-when-cross-origin"

    ErrorLog  /proc/self/fd/2
    CustomLog /proc/self/fd/1 combined
</VirtualHost>
