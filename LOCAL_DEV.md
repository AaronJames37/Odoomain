# Local development setup

Goal: run VS Code, Claude Code and Odoo **on your local machine**, and keep the
server for running Odoo only. The production box is moving to 4 GB RAM, where a
remote VS Code session (~2.4 GB) plus Odoo (~1.3 GB) does not fit.

Workflow after setup:

```
edit locally -> commit -> push -> ssh <vps> 'bash /opt/odoo/scripts/deploy.sh'
```

Production facts this must match: **Odoo 19 Enterprise**, **Python 3.12**,
**PostgreSQL 16**, single database `cutmyplastic`.

---

## 1. Clone the repo

```bash
git clone https://github.com/AaronJames37/Odoomain.git
cd Odoomain
git checkout backup/live-addons-2026-07-04
```

This gives you the 11 custom addons. **For editing code, this step alone is
enough** — you don't need a running Odoo to work with Claude Code.

## 2. Install Docker

Docker Desktop (macOS/Windows) or Docker Engine + compose plugin (Linux).

## 3. Copy the Enterprise addons from the server

Odoo Enterprise source is licensed and is deliberately **not** in this repo.
Only 55 enterprise-only modules are actually installed (~225 MB), not the whole
2.2 GB addons tree.

From your local machine, in the repo root:

```bash
mkdir -p enterprise-addons
# Pull just the enterprise modules the database actually uses.
ssh root@<vps-ip> 'tar czf - -C /usr/lib/python3/dist-packages/odoo/addons \
  $(psql "postgresql://odoo:odoo_password@localhost/cutmyplastic" -tAc \
    "SELECT name FROM ir_module_module WHERE state='"'"'installed'"'"'" \
    | while read m; do \
        [ -d "/usr/lib/python3/dist-packages/odoo/addons/$m" ] && \
        [ ! -d "/opt/odoo-source/addons/$m" ] && \
        [ ! -d "/opt/odoo/addons/$m" ] && echo "$m"; \
      done | tr "\n" " ")' | tar xzf - -C enterprise-addons
```

Simpler alternative if that one-liner is fiddly — copy the whole tree (2.2 GB):

```bash
rsync -az --info=progress2 \
  root@<vps-ip>:/usr/lib/python3/dist-packages/odoo/addons/ \
  enterprise-addons/
```

`enterprise-addons/` is gitignored — licensed source stays out of git.

## 4. Start the stack

```bash
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f odoo   # watch it boot
```

Odoo: <http://localhost:8069> · Postgres is on host port **5433**
(5433, not 5432, so it won't clash with a local Postgres).

## 5. Seed it with production data

Grab a dump from the server (these are made by the backup step):

```bash
scp root@<vps-ip>:/root/odoo-ram-backup-*/cutmyplastic.dump .
scp root@<vps-ip>:/root/odoo-ram-backup-*/filestore-cutmyplastic.tar.gz .

scripts/restore-local.sh cutmyplastic.dump filestore-cutmyplastic.tar.gz
```

The restore script **neutralises** the copy: outbound mail servers off, all
crons disabled, `web.base.url` pointed at localhost. That matters — an
un-neutralised copy of production will happily email real customers.

To take a fresh dump from the server later:

```bash
ssh root@<vps-ip> 'sudo -u postgres pg_dump -Fc cutmyplastic \
  -f /var/lib/postgresql/latest.dump'
scp root@<vps-ip>:/var/lib/postgresql/latest.dump .
```

## 6. Deploying changes

```bash
git add -A && git commit -m "..." && git push
ssh root@<vps-ip> 'bash /opt/odoo/scripts/deploy.sh'
```

When you've changed **models, views, or security rules**, upgrade the module so
Odoo reloads its schema and XML:

```bash
ssh root@<vps-ip> 'bash /opt/odoo/scripts/deploy.sh -u tp_sheet_nesting'
ssh root@<vps-ip> 'bash /opt/odoo/scripts/deploy.sh -u all'
```

A plain restart (no `-u`) is enough for pure Python logic changes.

---

## Gotchas

- **`/etc/odoo/odoo.conf` must stay `odoo:odoo`.** Some editors rewrite it as
  root, and Odoo then fails to start with *"config file ... doesn't exist or is
  not readable"*. Fix: `chown odoo:odoo /etc/odoo/odoo.conf`.
- **Calendar and CRM are coupled.** `crm` hard-depends on `calendar` in Odoo 19,
  so uninstalling Calendar removes CRM too. Reinstalling CRM pulls Calendar back.
- **Local runs threaded (`workers = 0`)**, production runs `workers = 2`. Don't
  copy the local config to the server — the low-RAM tuning there is deliberate.
- **The production box has no swap yet.** The 4 GB server needs a 4 GB swap file
  created (`fallocate` / `mkswap` / `swapon` + an `/etc/fstab` entry);
  `/etc/sysctl.d/99-odoo-swap.conf` already sets `vm.swappiness=10`.
