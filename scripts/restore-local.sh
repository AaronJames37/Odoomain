#!/usr/bin/env bash
# Seed the LOCAL docker stack with a copy of production.
#
# Run on your local machine, from the repo root, after:
#   1. docker compose -f docker/docker-compose.yml up -d
#   2. scp'ing the dump + filestore tarball from the server
#
# Usage:
#   scripts/restore-local.sh cutmyplastic.dump filestore-cutmyplastic.tar.gz

set -euo pipefail

# Git Bash (MSYS) rewrites container-absolute paths like /var/lib/odoo into
# C:\var\lib\odoo before they reach docker. That broke the filestore step and,
# because of set -e, skipped neutralisation entirely — leaving a live copy of
# production with crons and mail enabled. Disable the rewriting.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

DUMP="${1:?usage: restore-local.sh <db.dump> [filestore.tar.gz]}"
FILESTORE="${2:-}"
DB=cutmyplastic
COMPOSE="docker compose -f docker/docker-compose.yml"

[ -f "$DUMP" ] || { echo "no such dump: $DUMP" >&2; exit 1; }

echo "==> Dropping and recreating $DB"
$COMPOSE exec -T db dropdb -U odoo --if-exists "$DB"
$COMPOSE exec -T db createdb -U odoo "$DB"

echo "==> Restoring $DUMP (this takes a minute)"
# --no-owner: the prod role may not exist locally.
$COMPOSE exec -T db pg_restore -U odoo -d "$DB" --no-owner --no-privileges < "$DUMP" \
  2>&1 | grep -vE 'warning: (errors ignored|no privileges)' || true

echo "==> Neutralising the database (disable outbound mail, crons, prod URLs)"
# Runs BEFORE the filestore copy, on purpose: this is the step that stops a
# local copy emailing real customers, so it must not be skippable by a later
# failure. Do not move it back down.
$COMPOSE exec -T db psql -U odoo -d "$DB" <<'SQL'
UPDATE ir_mail_server SET active = false;
UPDATE ir_cron SET active = false;
UPDATE ir_config_parameter SET value = 'http://localhost:8069'
 WHERE key = 'web.base.url';
SQL

if [ -n "$FILESTORE" ] && [ -f "$FILESTORE" ]; then
  echo "==> Restoring filestore"
  # Odoo expects <data_dir>/filestore/<dbname>. In the odoo:19 image data_dir is
  # /var/lib/odoo/.local/share/Odoo — NOT /var/lib/odoo. Putting the filestore
  # at /var/lib/odoo/filestore silently breaks every attachment and asset
  # bundle (500s on /web/assets/..., login form renders blank).
  CID="$($COMPOSE ps -q odoo)"
  FS_ROOT=/var/lib/odoo/.local/share/Odoo/filestore
  docker exec -u root "$CID" mkdir -p "$FS_ROOT"
  docker exec -u root "$CID" rm -rf "$FS_ROOT/$DB"
  rm -rf "/tmp/$DB"
  tar xzf "$FILESTORE" -C /tmp
  docker cp "/tmp/$DB" "$CID:$FS_ROOT/$DB"
  # -u root: docker cp lands files as root, and the container's default odoo
  # user cannot chown them.
  docker exec -u root "$CID" chown -R odoo:odoo /var/lib/odoo/.local
  rm -rf "/tmp/$DB"

  # Compiled asset bundles from production reference filestore entries that
  # were not part of the dump. Drop them; Odoo recompiles from source on the
  # next request.
  $COMPOSE exec -T db psql -U odoo -d "$DB" -c \
    "DELETE FROM ir_attachment WHERE res_model='ir.ui.view' AND (name LIKE '%.assets_%' OR url LIKE '/web/assets/%');"
else
  echo "==> No filestore given — attachments/images will be missing (that's fine for code work)"
fi

echo "==> Verifying neutralisation"
# Loading the registry can re-activate crons via module init hooks, so assert
# the end state rather than trusting the earlier UPDATE.
$COMPOSE exec -T db psql -U odoo -d "$DB" <<'SQL'
UPDATE ir_mail_server SET active = false;
UPDATE ir_cron SET active = false;
SQL
LEFT="$($COMPOSE exec -T db psql -U odoo -d "$DB" -tAc \
  "SELECT (SELECT count(*) FROM ir_cron WHERE active)+(SELECT count(*) FROM ir_mail_server WHERE active);" | tr -d '\r')"
if [ "$LEFT" != "0" ]; then
  echo "!! WARNING: $LEFT cron(s)/mail server(s) still active — investigate before using this DB" >&2
  exit 1
fi
echo "    crons: 0 active, mail servers: 0 active, base url: localhost"

echo
echo "Done. Odoo: http://localhost:8069   (db: $DB)"
echo "Note: the DB is neutralised — crons and outbound mail are OFF."
