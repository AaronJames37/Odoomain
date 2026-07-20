#!/usr/bin/env bash
# Provision a fresh Ubuntu 24.04 box to run Odoo 19 Enterprise on 4GB RAM.
#
# Run as root on the NEW OVH server:
#   bash provision-ovh.sh
#
# Idempotent — safe to re-run. Does NOT touch data; see MIGRATION.md phase 2
# for restoring the database and filestore.

set -euo pipefail

DB_PASSWORD="${DB_PASSWORD:-odoo_password}"   # override: DB_PASSWORD=xxx bash provision-ovh.sh
DOMAIN="odoo.cutmyplastic.com.au"

log() { echo -e "\n==> $*"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

# ---------------------------------------------------------------- swap
# 4GB box with no swap will OOM-kill Odoo under a nesting solve.
log "Swap"
if swapon --show | grep -q .; then
  echo "    swap already present, skipping"
else
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "    4GB swapfile created"
fi

cat > /etc/sysctl.d/99-odoo-swap.conf <<'EOF'
# Prefer reclaiming page cache over swapping Odoo's hot pages.
vm.swappiness = 10
vm.vfs_cache_pressure = 50
EOF
sysctl -q -p /etc/sysctl.d/99-odoo-swap.conf

# ---------------------------------------------------------------- base
log "Base packages"
apt-get update -qq
apt-get install -y -qq curl wget gnupg ca-certificates nginx certbot \
                       python3-certbot-nginx postgresql postgresql-client git

# ---------------------------------------------------------------- postgres
log "PostgreSQL role"
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='odoo'" | grep -q 1; then
  echo "    role 'odoo' exists"
else
  sudo -u postgres psql -qc "CREATE ROLE odoo LOGIN CREATEDB PASSWORD '${DB_PASSWORD}';"
  echo "    role 'odoo' created"
fi

log "PostgreSQL tuning for 4GB"
sudo -u postgres psql -q <<'SQL'
ALTER SYSTEM SET max_connections = '50';
ALTER SYSTEM SET shared_buffers = '256MB';
ALTER SYSTEM SET effective_cache_size = '1GB';
ALTER SYSTEM SET work_mem = '8MB';
ALTER SYSTEM SET maintenance_work_mem = '64MB';
SQL
systemctl restart postgresql

# ---------------------------------------------------------------- odoo
log "Odoo 19 Enterprise"
if ! dpkg -l odoo >/dev/null 2>&1; then
  wget -qO- https://nightly.odoo.com/odoo.key | gpg --dearmor \
      > /etc/apt/trusted.gpg.d/odoo-archive-keyring.gpg
  cat > /etc/apt/sources.list.d/odoo.sources <<'EOF'
Types: deb
URIs: https://nightly.odoo.com/enterprise/19.0
Suites: ./
Signed-By: /etc/apt/trusted.gpg.d/odoo-archive-keyring.gpg
EOF
  apt-get update -qq
  apt-get install -y odoo
else
  echo "    odoo already installed ($(dpkg -s odoo | awk '/^Version/{print $2}'))"
fi

# ---------------------------------------------------------------- odoo.conf
# Low-RAM tuning, proven on the old box. workers=2 is the RAM-first choice:
# each worker costs ~200-250MB with this module set.
log "odoo.conf"
cat > /etc/odoo/odoo.conf <<EOF
[options]
db_host = localhost
db_port = 5432
db_user = odoo
db_password = ${DB_PASSWORD}
addons_path = /usr/lib/python3/dist-packages/odoo/addons,/opt/odoo/addons
proxy_mode = True
workers = 2
max_cron_threads = 1
gevent_port = 8072
limit_time_cpu = 600
limit_time_real = 1200
limit_time_real_cron = 1200
limit_request = 8192
limit_memory_soft = 1073741824
limit_memory_hard = 1342177280
EOF
# Odoo runs as the 'odoo' user and must be able to READ this file.
chown odoo:odoo /etc/odoo/odoo.conf
chmod 0640 /etc/odoo/odoo.conf

# ---------------------------------------------------------------- logrotate
# The stock config has no rotate/maxsize and grows unbounded.
log "logrotate"
cat > /etc/logrotate.d/odoo <<'EOF'
/var/log/odoo/*.log {
    daily
    rotate 7
    maxsize 20M
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su odoo odoo
}
EOF

log "journald size cap"
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\nSystemMaxFileSize=50M\n' \
    > /etc/systemd/journald.conf.d/99-size-limit.conf
systemctl restart systemd-journald

# ---------------------------------------------------------------- nginx
# Plain HTTP for now; certbot rewrites this to 443 after DNS points here.
# NOTE: the old box also proxied /mcp-agent/ to :8008 — deliberately dropped,
# that service is gone.
log "nginx"
cat > /etc/nginx/sites-available/odoo <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    proxy_read_timeout 720s;
    proxy_connect_timeout 720s;
    proxy_send_timeout 720s;
    client_max_body_size 200m;

    location /websocket {
        proxy_pass http://127.0.0.1:8072;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 720s;
        proxy_send_timeout 720s;
        proxy_buffering off;
    }

    location / {
        proxy_pass http://127.0.0.1:8069;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
ln -sf /etc/nginx/sites-available/odoo /etc/nginx/sites-enabled/odoo
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# ---------------------------------------------------------------- done
systemctl enable --now odoo >/dev/null 2>&1 || systemctl restart odoo

log "Done"
cat <<EOF

  swap:       $(free -h | awk '/Swap:/{print $2}')
  postgres:   $(systemctl is-active postgresql)
  odoo:       $(systemctl is-active odoo)
  nginx:      $(systemctl is-active nginx)

Next (see MIGRATION.md):
  phase 2 - clone addons into /opt/odoo, restore DB + filestore
  phase 3 - test via a hosts-file override before flipping DNS
  phase 4 - final sync, flip DNS, then: certbot --nginx -d ${DOMAIN}

Odoo has no database yet — it will show the database manager until you restore.
EOF
