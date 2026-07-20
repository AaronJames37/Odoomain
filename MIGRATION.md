# Migration: DigitalOcean → OVHcloud

Staged cutover. Build and verify the new box first; DigitalOcean stays live and
becomes the rollback until DNS is flipped and confirmed.

**Source:** DigitalOcean, `170.64.227.145`, Ubuntu 24.04, 8 GB RAM
**Target:** OVHcloud, Ubuntu 24.04 (reinstalled to match), 4 GB RAM + 4 GB swap
**Domain:** `odoo.cutmyplastic.com.au`

Payload is small — DB 149 MB, filestore 100 MB, addons 10 MB (in git).

---

## Phase 0 — Before you start (do now, ~5 min)

**Lower the DNS TTL.** This is the one step that must happen *early*: TTL changes
only take effect after the old TTL expires, so doing it now shrinks the cutover
window later.

In your DNS provider, set the A record for `odoo.cutmyplastic.com.au` to
**TTL 300** (5 min). Leave the IP pointing at DigitalOcean.

Note the OVH IP once provisioned — referred to below as `<OVH_IP>`.

---

## Phase 1 — Build the OVH box (no downtime, DO stays live)

SSH in as root with your `aaron@odoo` key, then run
`scripts/provision-ovh.sh` from this repo (copy it over, or paste it).

It performs, in order:

1. **Swap** — 4 GB swapfile + `/etc/fstab` entry, `vm.swappiness=10`.
   *The DO box never had swap; on 4 GB it is not optional.*
2. **PostgreSQL 16** + an `odoo` role.
3. **Odoo 19 Enterprise** — see the warning below; the nightly repo is dead and
   the package is repacked from the old box instead.
4. **nginx + certbot** (cert issued later, after DNS points here).
5. **Low-RAM tuning** — the same `odoo.conf` and PostgreSQL settings already
   proven on the DO box (`workers=2`, memory limits, `max_connections=50`).

### The Enterprise apt repo is gone (discovered 2026-07-20)

`https://nightly.odoo.com/enterprise/19.0` returns **404**. Verified from the OVH
box, the DigitalOcean box, and locally — it is an upstream change, not a
credentials or firewall issue. The Community repo still works, but Community is
not what production runs.

It also breaks `apt-get update` on **both** boxes while the source file is
present, which makes unrelated installs fail with a confusing
*"Unable to locate package"*. The source files have been renamed `.disabled` on
both (originals backed up on DO at `/root/repo-backup/`).

**What was actually done (works, ~40 s for 1.9 GB):** rsync the installed tree
straight from the old box. `dpkg-repack` also works but spends ~15 min
compressing 2.2 GB before you can even start copying, so it was abandoned.

```bash
# 1. Runtime deps from the stock Ubuntu archive (all ~48 resolve — verified)
sudo apt-get install -y python3-{asn1crypto,babel,cbor2,chardet,cryptography,\
dateutil,docutils,geoip2,gevent,greenlet,idna,jinja2,libsass,lxml-html-clean,\
markupsafe,num2words,ofxparse,openpyxl,openssl,passlib,pil,polib,psutil,\
psycopg2,pypdf2,qrcode,reportlab,requests,rjsmin,serial,stdnum,tz,urllib3,usb,\
vobject,werkzeug,xlrd,xlsxwriter,xlwt,zeep,freezegun,magic,renderpm} \
  fonts-{inconsolata,font-awesome,roboto-unhinted,dejavu-core} \
  gsfonts libjs-underscore postgresql-client

# 2. The odoo system user (the tree alone does not create it)
sudo adduser --system --quiet --home /var/lib/odoo --group odoo
sudo mkdir -p /var/lib/odoo /var/log/odoo /etc/odoo
sudo chown -R odoo:odoo /var/lib/odoo /var/log/odoo

# 3. The code — from a temporary key on the OLD box (delete it afterwards)
rsync -a -e "ssh -i /root/.ssh/migrate_key" \
  /usr/lib/python3/dist-packages/odoo/ \
  root@<OVH_IP>:/usr/lib/python3/dist-packages/odoo/
rsync -a -e "ssh -i /root/.ssh/migrate_key" \
  /usr/bin/odoo /lib/systemd/system/odoo.service root@<OVH_IP>:/tmp/odoo-bits/

# 4. Place binary + unit, then start
sudo install -m 755 /tmp/odoo-bits/odoo /usr/bin/odoo
sudo install -m 644 /tmp/odoo-bits/odoo.service /lib/systemd/system/odoo.service
sudo systemctl daemon-reload && sudo systemctl enable --now odoo
```

**Do not copy `/etc/init.d/odoo` across.** The sysv script has no runlevels and
makes `systemctl enable` abort with *"Default-Start contains no runlevels"*.
systemd alone is what this box needs.

`provision-ovh.sh` also accepts a repacked `.deb` at `/tmp/odoo-enterprise.deb`
(override with `ODOO_DEB=`) if you ever prefer the package route.

Confirm Enterprise, not Community — the `+e` is the whole point:

```bash
grep "version +=" /usr/lib/python3/dist-packages/odoo/release.py   # +e-20260323
```

Verify before continuing:

```bash
free -h                      # swap present
systemctl is-active postgresql odoo
psql --version               # 16.x
dpkg -s odoo | grep Version  # expect 19.0+e.*  ('+e' = Enterprise)
```

## Phase 2 — Seed data (no downtime)

Clone the addons and restore a dump. **From the OVH box:**

```bash
mkdir -p /opt/odoo && cd /opt
git clone https://github.com/AaronJames37/Odoomain.git odoo
cd odoo && git checkout backup/live-addons-2026-07-04
chown -R odoo:odoo /opt/odoo/addons
```

Pull a dump from DO (run on OVH; needs the DO key, or push from your PC):

```bash
scp root@170.64.227.145:/root/local-dev-bundle/cutmyplastic.dump /tmp/
scp root@170.64.227.145:/root/local-dev-bundle/filestore-cutmyplastic.tar.gz /tmp/

sudo -u postgres createdb -O odoo cutmyplastic
sudo -u postgres pg_restore -d cutmyplastic --no-owner --no-privileges /tmp/cutmyplastic.dump

# CRITICAL: restoring as the postgres superuser with --no-owner leaves every
# object owned by 'postgres', not 'odoo'. Odoo then fails on EVERY request with
#   psycopg2.errors.DuplicateTable: relation "orm_signaling_registry" already exists
# because setup_signaling() cannot use a table it can see but does not own.
# The symptom is a /web/login -> /web/login redirect loop and "Failed to load
# registry" in the log. Hit this for real on 2026-07-20. Always run this after
# a restore:
sudo -u postgres psql -d cutmyplastic <<'SQL'
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' AND tableowner<>'odoo' LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO odoo', r.tablename);
  END LOOP;
  FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE c.relkind='S' AND n.nspname='public' AND pg_get_userbyid(c.relowner)<>'odoo' LOOP
    EXECUTE format('ALTER SEQUENCE public.%I OWNER TO odoo', r.relname);
  END LOOP;
  FOR r IN SELECT viewname FROM pg_views WHERE schemaname='public' AND viewowner<>'odoo' LOOP
    EXECUTE format('ALTER VIEW public.%I OWNER TO odoo', r.viewname);
  END LOOP;
END $$;
SQL
# Verify — all three must be 0:
sudo -u postgres psql -d cutmyplastic -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner<>'odoo'"

mkdir -p /var/lib/odoo/.local/share/Odoo/filestore
tar xzf /tmp/filestore-cutmyplastic.tar.gz \
    -C /var/lib/odoo/.local/share/Odoo/filestore
chown -R odoo:odoo /var/lib/odoo
systemctl restart odoo
```

**`REASSIGN OWNED BY postgres TO odoo` does not work** — Postgres refuses it for
a superuser role ("required by the database system"). The per-object loop above
is the way.

### Keep crons off while testing, without neutralising the data

The restored copy has **39 active crons and a live SMTP server** — correct for
production, dangerous on a box that is not live yet. Set this in
`/etc/odoo/odoo.conf` for Phase 3:

```
max_cron_threads = 0
```

That stops cron execution at the *server* level and leaves `ir_cron` rows
untouched, so there is nothing to undo at cutover — just set it back to `1`.
Do **not** run `restore-local.sh` here: it disables mail servers and crons in
the database, which is right for the local Docker stack and wrong for this box.

## Phase 3 — Test on the new box (no downtime)

DNS still points at DO, so reach OVH directly. On your PC, add to
`C:\Windows\System32\drivers\etc\hosts` (as administrator):

```
<OVH_IP>  odoo.cutmyplastic.com.au
```

Then browse to the site — you are hitting OVH while everyone else still hits DO.

**Test properly. This is the whole point of a staged cutover:**

- [ ] Log in
- [ ] Open a sale order (360 exist) — check attachments/images load (filestore)
- [ ] Run a nesting job end-to-end (the custom kernel is the highest-risk code)
- [ ] Open Accounting → check a report renders
- [ ] Check `/var/log/odoo/odoo-server.log` for tracebacks
- [ ] `free -h` under load — confirm RAM sits within 4 GB

Remove the hosts entry when done.

## Phase 4 — Cutover (downtime ≈ 10–15 min)

Pick a quiet window. **Take the last sync at the moment of cutover** — anything
entered on DO after the Phase 2 dump would otherwise be lost.

```bash
# 1. ON DO — stop Odoo so no further writes land
systemctl stop odoo

# 2. ON DO — final dump
sudo -u postgres pg_dump -Fc cutmyplastic -f /var/lib/postgresql/final.dump
tar czf /root/final-filestore.tar.gz \
    -C /var/lib/odoo/.local/share/Odoo/filestore cutmyplastic

# 3. ON OVH — pull and restore over the test data
scp root@170.64.227.145:/var/lib/postgresql/final.dump /tmp/
scp root@170.64.227.145:/root/final-filestore.tar.gz /tmp/
systemctl stop odoo
sudo -u postgres dropdb cutmyplastic
sudo -u postgres createdb -O odoo cutmyplastic
sudo -u postgres pg_restore -d cutmyplastic --no-owner --no-privileges /tmp/final.dump

# MUST re-run the ownership fix — see Phase 2. Without it Odoo will not serve a
# single request, and you are inside the downtime window when you find out.
sudo -u postgres psql -d cutmyplastic <<'SQL'
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' AND tableowner<>'odoo' LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO odoo', r.tablename);
  END LOOP;
  FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE c.relkind='S' AND n.nspname='public' AND pg_get_userbyid(c.relowner)<>'odoo' LOOP
    EXECUTE format('ALTER SEQUENCE public.%I OWNER TO odoo', r.relname);
  END LOOP;
  FOR r IN SELECT viewname FROM pg_views WHERE schemaname='public' AND viewowner<>'odoo' LOOP
    EXECUTE format('ALTER VIEW public.%I OWNER TO odoo', r.viewname);
  END LOOP;
END $$;
SQL

rm -rf /var/lib/odoo/.local/share/Odoo/filestore/cutmyplastic
tar xzf /tmp/final-filestore.tar.gz -C /var/lib/odoo/.local/share/Odoo/filestore
chown -R odoo:odoo /var/lib/odoo

# Re-enable crons — they were turned off at the server level for Phase 3 testing.
sed -i 's/^max_cron_threads = .*/max_cron_threads = 1/' /etc/odoo/odoo.conf
chown odoo:odoo /etc/odoo/odoo.conf && chmod 0640 /etc/odoo/odoo.conf

systemctl start odoo
```

**4. Flip DNS** — point the A record at `<OVH_IP>`. With TTL 300 it propagates in
~5 minutes.

**5. Issue the SSL cert** (only works once DNS resolves to OVH):

```bash
certbot --nginx -d odoo.cutmyplastic.com.au
```

**6. Verify:**

```bash
curl -I https://odoo.cutmyplastic.com.au        # expect 200/303
systemctl is-active odoo postgresql nginx
```

## Phase 5 — After cutover

- Leave the DO droplet **powered on but with Odoo stopped** for ~48 h as rollback.
  To roll back: flip DNS to `170.64.227.145` and `systemctl start odoo`.
- Once confident, snapshot the droplet, then destroy it.
- Point `scripts/deploy.sh` at the new IP (or use a hostname).

---

## Differences from the old box — deliberate

- **`/mcp-agent/` nginx block is dropped.** It proxied to port 8008 for the
  `mcp-odoo` service, which has been disabled and removed. Do not carry it over.
- **Swap now exists** (4 GB). The DO box had none.
- **No Docker.** It was installed on DO but unused; the local dev stack runs on
  your PC, not the server.
- **No VS Code Server.** That is the point of the local-dev move — it was ~2.4 GB
  RAM and 1.7 GB disk.

## Gotchas

- **`/etc/odoo/odoo.conf` must be owned `odoo:odoo` (0640).** If Odoo fails with
  *"config file … doesn't exist or is not readable"*, this is why.
- **Restore before issuing the cert.** Certbot needs DNS pointing at OVH, so it
  is a post-flip step; nginx serves plain HTTP until then.
- **Enterprise subscription** is recorded in the database
  (`database.enterprise_code`, expires 2027-04-08) and travels with the dump —
  nothing extra to move, but the DB UUID changes on restore, so Odoo may ask you
  to re-validate the subscription. Keep the subscription code to hand.
- **Don't skip the final sync.** The Phase 2 dump is for testing only; orders
  placed between then and cutover live only on DO.
