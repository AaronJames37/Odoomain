# Handoff: VPS → local development

Written by Claude running on the Odoo VPS (2026-07-20) for Claude running on
Aaron's local Windows PC. Everything below was verified on the server, not
assumed.

## What this repo is

Custom Odoo 19 **Enterprise** addons for a plastic-cutting business
(cutmyplastic). 11 custom modules, the significant ones being the sheet-nesting
suite.

- Remote: `github.com/AaronJames37/Odoomain`
- Working branch: **`backup/live-addons-2026-07-04`** (not `master`; master is
  stale at Jul 4)
- HEAD at handoff: `dbc399f`, clean tree

Custom addons: `tp_sheet_nesting`, `tp_sheet_nesting_run`,
`tp_sheet_nesting_processing_view`, `tp_sheet_nesting_sandbox`,
`tp_offcuts_nesting`, `tp_storefront_manager`, `website_fulfillment_status_sync`,
`stripe_gross_import`, `paypal_gross_import`, `ebay_gross_import`, `mcp_server`.

## Why the move to local

The production VPS is migrating to a **4 GB RAM** box. VS Code Remote + language
servers + AI extensions were measured at **~2.4 GB RAM and 1.7 GB disk** on the
server — that does not fit alongside Odoo (~0.9 GB) and PostgreSQL (~0.4 GB).

Target workflow:

```
edit locally → commit → push → ssh <vps> 'bash /opt/odoo/scripts/deploy.sh'
```

The VPS runs Odoo only. No editor, no language server, no AI tooling.

## Where Aaron got to

Done:
- Work committed and pushed (`ea754b7`: 18 files, 1,149 insertions of nesting work
  that existed **only** on the VPS — that risk is now closed).
- Local-dev scaffold committed (`dbc399f`): `docker/`, `scripts/`, `LOCAL_DEV.md`.
- Repo cloned to `C:\dev\Odoomain`, branch checked out.
  *(An earlier clone landed in `C:\Windows\System32\Odoomain` — should be deleted
  if it still exists.)*
- Server disk cleaned: 96% → 51% used (~11 GB reclaimed).

**Blocked here:** pulling the prepared bundle off the VPS. Aaron doesn't know the
root password, and `PasswordAuthentication no` is set — SSH key is required.

## Immediate next step

A verified 79 MB bundle is staged on the VPS at `/root/local-dev-bundle/`:

| File | Size | What |
|---|---|---|
| `cutmyplastic.dump` | 11 MB | Postgres custom-format dump, 15,264 TOC entries |
| `enterprise-addons.tar.gz` | 42 MB | The 55 Enterprise-only modules actually installed |
| `filestore-cutmyplastic.tar.gz` | 27 MB | Attachments (860 records) |

VPS IP: **170.64.227.145**, user `root`.

The authorized key is ED25519, fingerprint
`SHA256:b7qrpmmt34j5L6IYdGiV8MwbnGtUjmuREztYlbf7Be4`, comment `aaron@odoo`.
Find the matching private key on Windows:

```cmd
dir %USERPROFILE%\.ssh
ssh-keygen -lf %USERPROFILE%\.ssh\id_ed25519
type %USERPROFILE%\.ssh\config
```

Then, from `C:\dev\Odoomain`:

```cmd
scp -i %USERPROFILE%\.ssh\<key> -r root@170.64.227.145:/root/local-dev-bundle bundle
mkdir enterprise-addons
tar -xzf bundle\enterprise-addons.tar.gz -C enterprise-addons
docker compose -f docker/docker-compose.yml up -d
```

Then restore (needs **Git Bash**, the script is bash):

```bash
cd /c/dev/Odoomain
cp bundle/cutmyplastic.dump bundle/filestore-cutmyplastic.tar.gz .
scripts/restore-local.sh cutmyplastic.dump filestore-cutmyplastic.tar.gz
```

`restore-local.sh` **neutralises** the copy — disables outbound mail servers, all
crons, and repoints `web.base.url` at localhost. Do not skip this; an
un-neutralised production copy will email real customers when a cron fires.

## Environment facts (must match)

| | Production | Local (docker/) |
|---|---|---|
| Odoo | 19.0 Enterprise | `odoo:19` image |
| Python | 3.12.3 | image default |
| PostgreSQL | 16.14 | `postgres:16`, host port **5433** |
| workers | **2** (RAM-tuned) | **0** (threaded, for debugging) |
| DB | `cutmyplastic`, 155 modules | same |

Enterprise source is licensed and deliberately **not** in git —
`enterprise-addons/` is gitignored and must be copied from the server.

## Gotchas discovered the hard way

- **`/etc/odoo/odoo.conf` must be owned `odoo:odoo`.** An edit made it
  `root:root` and Odoo failed to boot with *"config file … doesn't exist or is
  not readable"*. If Odoo won't start after a config change, check this first.
- **`crm` hard-depends on `calendar`** in Odoo 19. Uninstalling Calendar silently
  removed CRM too. (No data was lost — CRM was already unused — but don't assume
  Calendar is standalone.)
- **Do not copy the local `odoo.conf` to the server.** Production's
  `workers=2` + memory limits are deliberate for a 4 GB box.
- **The nesting kernel no longer uses OR-Tools/CP-SAT.** It's a custom kernel;
  the addons import no numpy/ortools/scipy. A stale 244 MB `nestvenv` was deleted.
- **Windows:** `cat` isn't cmd — use `type`. The `.sh` scripts need Git Bash or
  WSL2; `tar` is built into modern Windows cmd.

## Production state (post-tuning, for reference)

`odoo.conf`: `workers=2`, `limit_memory_soft=1.0 GB`, `limit_memory_hard=1.28 GB`,
`limit_request=8192`, `limit_time_cpu=600`, `limit_time_real=1200`.
PostgreSQL: `max_connections=50`, `shared_buffers=256MB`,
`effective_cache_size=1GB`, `work_mem=8MB`.

Modules trimmed 242 → 155 (removed unused Enterprise apps: payroll, survey,
mass-mailing, sign, appointment, planning, social, project, hr, calendar,
stock_barcode, mrp_workorder — all verified zero-data first). Dashboards kept.

## Outstanding

1. **The 4 GB server has no swap yet.** Create a 4 GB swapfile + `/etc/fstab`
   entry on the new box; `/etc/sysctl.d/99-odoo-swap.conf` already sets
   `vm.swappiness=10`.
2. **Rotate the GitHub PAT.** A fine-grained token was pasted into the VPS chat
   session; it expires 2026-08-19 and should be revoked sooner.
3. **SSH is being brute-forced** (24 MB of failed-login records) and
   `PermitRootLogin yes` is set. Switch to key-only + consider fail2ban.
4. **The working branch is unmerged** — `backup/live-addons-2026-07-04` is ahead
   of `master`. Open a PR when convenient.
5. Once local dev is confirmed working, `/root/.vscode-server` (1.7 GB) can be
   removed from the VPS.
