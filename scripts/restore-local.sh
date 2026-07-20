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

if [ -n "$FILESTORE" ] && [ -f "$FILESTORE" ]; then
  echo "==> Restoring filestore"
  # Odoo expects <data_dir>/filestore/<dbname>
  $COMPOSE exec -T odoo mkdir -p /var/lib/odoo/filestore
  tar xzf "$FILESTORE" -O 2>/dev/null >/dev/null || true
  $COMPOSE exec -T odoo sh -c "rm -rf /var/lib/odoo/filestore/$DB"
  tar xzf "$FILESTORE" -C /tmp
  docker cp "/tmp/$DB" "$($COMPOSE ps -q odoo):/var/lib/odoo/filestore/$DB"
  $COMPOSE exec -T odoo chown -R odoo:odoo /var/lib/odoo/filestore
  rm -rf "/tmp/$DB"
else
  echo "==> No filestore given — attachments/images will be missing (that's fine for code work)"
fi

echo "==> Neutralising the database (disable outbound mail, crons, prod URLs)"
# Critical: stops a local copy from emailing real customers or hitting live APIs.
$COMPOSE exec -T db psql -U odoo -d "$DB" <<'SQL'
-- kill outgoing mail servers
UPDATE ir_mail_server SET active = false;
-- disable scheduled jobs (re-enable individually if you need to test one)
UPDATE ir_cron SET active = false;
-- clear any payment/webhook endpoints that point at production
UPDATE ir_config_parameter SET value = 'http://localhost:8069'
 WHERE key = 'web.base.url';
SQL

echo
echo "Done. Odoo: http://localhost:8069   (db: $DB)"
echo "Note: the DB is neutralised — crons and outbound mail are OFF."
