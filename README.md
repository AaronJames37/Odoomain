# Odoo 19 Enterprise Installation

## Overview
This is a fresh Odoo 19 Enterprise installation with custom manufacturing addons for sheet cutting and offcut management.

## Access
- **Web Interface**: http://your-droplet-ip:8069
- **Admin Password**: `NMN#Pc4=m6yP^-E`
- **Database**: PostgreSQL 16 with user `odoo` / password `odoo_password`

## Custom Addons
Located in `/opt/odoo/addons/`:

1. **tp_offcuts_nesting** (v19.0.6.0.0)
   - Offcut inventory, valuation, and waste tracking
   - Dependencies: base, mrp, stock, account

2. **tp_sheet_nesting** (v19.0.1.0.0)
   - Cut list to MO nesting orchestration and optimization
   - Dependencies: base, sale, sale_mrp, mrp, stock, account, tp_offcuts_nesting

3. **tp_storefront_manager** (v19.0.1.0.0)
   - Manage headless storefront settings and appearance
   - Dependencies: base, product, tp_offcuts_nesting

## Services
```bash
# Check status
systemctl status odoo
systemctl status postgresql

# Restart services
systemctl restart odoo
systemctl restart postgresql

# View logs
tail -f /var/log/odoo/odoo-server.log
```

## Configuration
- **Config File**: `/etc/odoo/odoo.conf`
- **Addons Path**: `/usr/lib/python3/dist-packages/odoo/addons,/opt/odoo/addons`
- **Data Directory**: `/var/lib/odoo`

## Development
- Custom addons are in `/opt/odoo/addons/`
- Git repository: https://github.com/AaronJames37/Odoomain
- To update addons: `cd /opt/odoo && git pull && systemctl restart odoo`

## Backup
```bash
# Database backup
sudo -u postgres pg_dump -U odoo -h localhost your_database > backup.sql

# Full backup (includes addons)
tar -czf odoo_backup.tar.gz /opt/odoo /etc/odoo /var/lib/odoo
```

## Troubleshooting
- Check logs: `tail -50 /var/log/odoo/odoo-server.log`
- Test database: `sudo -u postgres psql -U odoo -h localhost -d postgres`
- Restart if needed: `systemctl restart odoo`