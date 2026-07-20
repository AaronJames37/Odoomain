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
3. **Odoo 19 Enterprise** from `https://nightly.odoo.com/enterprise/19.0`.
4. **nginx + certbot** (cert issued later, after DNS points here).
5. **Low-RAM tuning** — the same `odoo.conf` and PostgreSQL settings already
   proven on the DO box (`workers=2`, memory limits, `max_connections=50`).

Verify before continuing:

```bash
free -h                      # swap present
systemctl is-active postgresql odoo
psql --version               # 16.x
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

sudo -u postgres createdb cutmyplastic
sudo -u postgres pg_restore -d cutmyplastic --no-owner --no-privileges /tmp/cutmyplastic.dump

mkdir -p /var/lib/odoo/.local/share/Odoo/filestore
tar xzf /tmp/filestore-cutmyplastic.tar.gz \
    -C /var/lib/odoo/.local/share/Odoo/filestore
chown -R odoo:odoo /var/lib/odoo
systemctl restart odoo
```

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
sudo -u postgres createdb cutmyplastic
sudo -u postgres pg_restore -d cutmyplastic --no-owner --no-privileges /tmp/final.dump
rm -rf /var/lib/odoo/.local/share/Odoo/filestore/cutmyplastic
tar xzf /tmp/final-filestore.tar.gz -C /var/lib/odoo/.local/share/Odoo/filestore
chown -R odoo:odoo /var/lib/odoo
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
