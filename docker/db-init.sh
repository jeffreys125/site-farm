#!/bin/bash
# =============================================================================
#  Runs once, on the very first start of the db container (empty data volume).
#
#  The MySQL image creates MYSQL_USER with privileges on MYSQL_DATABASE only --
#  in this stack that is the single database `wp_hub`. The web tier needs to
#  create one database per site (wp_diy, wp_pet, ...), so grant the pattern.
#
#  GRANT treats `_` as a single-character wildcard, so it is escaped: the
#  pattern `wp\_%` matches wp_diy, wp_pet ... but not wpxdiy.
#
#  NOTE: the MySQL entrypoint *sources* this file instead of executing it, so
#  no `set -euo pipefail` here -- those flags would leak into the entrypoint
#  and abort it on unrelated unbound variables (MYSQL_ONETIME_PASSWORD ...).
# =============================================================================

mysql --protocol=socket -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
GRANT ALL PRIVILEGES ON \`wp\\_%\`.* TO '${MYSQL_USER}'@'%';
FLUSH PRIVILEGES;
SQL

echo "[db-init] granted ${MYSQL_USER} privileges on wp_%"
