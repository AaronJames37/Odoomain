#!/usr/bin/env bash
# Deploy custom addons to the Odoo server.
#
# Run this ON THE SERVER (or via ssh) after pushing from local.
#
# Production moved to OVHcloud on 2026-07-20. That box has no root login —
# log in as 'ubuntu' and sudo. The old DigitalOcean host (170.64.227.145)
# is retained only as rollback and must NOT be deployed to.
#
#   ssh -i ~/.ssh/windows_key ubuntu@51.161.134.72 \
#       'sudo bash /opt/odoo/scripts/deploy.sh'
#
# Usage (all need root, so run under sudo):
#   deploy.sh                     pull + restart
#   deploy.sh -u tp_sheet_nesting pull + restart + upgrade that module
#   deploy.sh -u all              pull + restart + upgrade every custom module
#
# Upgrade (-u) is needed whenever models, views, or security rules change.
# A plain restart is enough for pure Python logic changes.

set -euo pipefail

REPO=/opt/odoo
DB=cutmyplastic
SERVICE=odoo
UPGRADE=""

while getopts "u:" opt; do
  case $opt in
    u) UPGRADE="$OPTARG" ;;
    *) echo "usage: $0 [-u module|all]" >&2; exit 1 ;;
  esac
done

# The custom modules living in this repo, comma-separated (for -u all).
custom_modules() {
  ls -1 "$REPO/addons" | tr '\n' ',' | sed 's/,$//'
}

echo "==> Pulling latest from git"
cd "$REPO"
BEFORE=$(git rev-parse --short HEAD)
git pull --ff-only
AFTER=$(git rev-parse --short HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
  echo "    already up to date ($AFTER)"
else
  echo "    $BEFORE -> $AFTER"
  git --no-pager log --oneline "$BEFORE..$AFTER" | sed 's/^/    /'
fi

# Odoo must be able to read the addons.
chown -R odoo:odoo "$REPO/addons"

if [ -n "$UPGRADE" ]; then
  [ "$UPGRADE" = "all" ] && UPGRADE=$(custom_modules)
  echo "==> Stopping Odoo to run upgrade: $UPGRADE"
  systemctl stop "$SERVICE"
  # --stop-after-init so this exits rather than holding the port.
  sudo -u odoo /usr/bin/odoo -c /etc/odoo/odoo.conf -d "$DB" \
      -u "$UPGRADE" --stop-after-init --no-http
  echo "==> Starting Odoo"
  systemctl start "$SERVICE"
else
  echo "==> Restarting Odoo"
  systemctl restart "$SERVICE"
fi

echo "==> Waiting for Odoo to answer"
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8069/web/login || true)
  if [ "$code" = "200" ]; then
    echo "    up (HTTP 200) after ${i}s"
    exit 0
  fi
  sleep 1
done

echo "!! Odoo did not return HTTP 200 in 30s — check the log:" >&2
echo "   tail -50 /var/log/odoo/odoo-server.log" >&2
exit 1
