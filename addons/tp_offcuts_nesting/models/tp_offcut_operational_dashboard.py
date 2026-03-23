from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class TpOffcutOperationalDashboard(models.Model):
    _name = "tp.offcut.operational.dashboard"
    _description = "TP Offcut Operational Dashboard"
    _rec_name = "company_id"

    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    as_of = fields.Datetime(default=fields.Datetime.now, readonly=True)

    total_offcut_count = fields.Integer(compute="_compute_metrics", readonly=True)
    available_offcut_count = fields.Integer(compute="_compute_metrics", readonly=True)
    reserved_offcut_count = fields.Integer(compute="_compute_metrics", readonly=True)
    in_use_offcut_count = fields.Integer(compute="_compute_metrics", readonly=True)
    sold_offcut_count = fields.Integer(compute="_compute_metrics", readonly=True)
    inactive_offcut_count = fields.Integer(compute="_compute_metrics", readonly=True)
    sold_cleanup_due_count = fields.Integer(compute="_compute_metrics", readonly=True)
    available_pct = fields.Float(compute="_compute_metrics", readonly=True)
    reserved_pct = fields.Float(compute="_compute_metrics", readonly=True)
    in_use_pct = fields.Float(compute="_compute_metrics", readonly=True)
    sold_pct = fields.Float(compute="_compute_metrics", readonly=True)
    inactive_pct = fields.Float(compute="_compute_metrics", readonly=True)
    waste_record_count_last_30d = fields.Integer(compute="_compute_metrics", readonly=True)
    inventory_value_total = fields.Monetary(
        compute="_compute_metrics",
        currency_field="currency_id",
        readonly=True,
    )
    waste_area_last_30d = fields.Float(compute="_compute_metrics", readonly=True)
    waste_value_last_30d = fields.Monetary(
        compute="_compute_metrics",
        currency_field="currency_id",
        readonly=True,
    )
    reserved_mo_count = fields.Integer(compute="_compute_metrics", readonly=True)
    currency_id = fields.Many2one(
        "res.currency",
        related="company_id.currency_id",
        readonly=True,
    )

    @api.constrains("company_id")
    def _check_unique_company(self):
        for record in self:
            duplicate = self.search(
                [("id", "!=", record.id), ("company_id", "=", record.company_id.id)],
                limit=1,
            )
            if duplicate:
                raise ValidationError("Only one dashboard record is allowed per company.")

    def _compute_metrics(self):
        Offcut = self.env["tp.offcut"].with_context(active_test=False).sudo()
        Waste = self.env["tp.offcut.waste"].sudo()
        now = fields.Datetime.now()
        for record in self:
            company_domain = [("company_id", "=", record.company_id.id)]
            offcuts = Offcut.search(company_domain)
            counts_by_state = {}
            for state_value, state_count in Offcut._read_group(
                domain=company_domain,
                groupby=["state"],
                aggregates=["__count"],
            ):
                if not state_value:
                    continue
                state_key = state_value[0] if isinstance(state_value, tuple) else state_value
                counts_by_state[state_key] = int(state_count)
            inventory_domain = company_domain + [("state", "in", ["available", "reserved", "in_use"])]
            inventory_value = sum(offcuts.filtered(lambda o: o.state in ("available", "reserved", "in_use")).mapped("remaining_value"))

            waste_cutoff = now - timedelta(days=30)
            waste_domain = [("timestamp", ">=", waste_cutoff), ("company_id", "=", record.company_id.id)]
            waste_rows = Waste.search(waste_domain)

            cleanup_cutoff = now - timedelta(days=max(int(record.company_id.tp_offcut_sold_cleanup_days or 30), 1))
            cleanup_domain = company_domain + [("state", "=", "sold"), ("sold_at", "!=", False), ("sold_at", "<=", cleanup_cutoff)]
            sold_bin = record.company_id.tp_offcut_sold_bin_location_id
            if sold_bin:
                cleanup_domain.append(("bin_location_id", "=", sold_bin.id))
            sold_cleanup_due = Offcut.search_count(cleanup_domain)

            reserved_mo_ids = offcuts.filtered(lambda o: o.state == "reserved" and o.reserved_mo_id).mapped("reserved_mo_id").ids

            record.total_offcut_count = len(offcuts)
            record.available_offcut_count = counts_by_state.get("available", 0)
            record.reserved_offcut_count = counts_by_state.get("reserved", 0)
            record.in_use_offcut_count = counts_by_state.get("in_use", 0)
            record.sold_offcut_count = counts_by_state.get("sold", 0)
            record.inactive_offcut_count = counts_by_state.get("inactive", 0)
            record.inventory_value_total = inventory_value
            record.waste_area_last_30d = sum(waste_rows.mapped("area_mm2"))
            record.waste_value_last_30d = sum(waste_rows.mapped("waste_value"))
            record.waste_record_count_last_30d = len(waste_rows)
            record.sold_cleanup_due_count = sold_cleanup_due
            record.reserved_mo_count = len(set(reserved_mo_ids))
            total = float(record.total_offcut_count or 0)
            record.available_pct = (record.available_offcut_count / total * 100.0) if total else 0.0
            record.reserved_pct = (record.reserved_offcut_count / total * 100.0) if total else 0.0
            record.in_use_pct = (record.in_use_offcut_count / total * 100.0) if total else 0.0
            record.sold_pct = (record.sold_offcut_count / total * 100.0) if total else 0.0
            record.inactive_pct = (record.inactive_offcut_count / total * 100.0) if total else 0.0

    def _tp_get_offcut_action(self, domain):
        self.ensure_one()
        action = self.env.ref("tp_offcuts_nesting.action_tp_offcut").read()[0]
        action["domain"] = domain
        action["context"] = dict(self.env.context)
        return action

    def action_open_total_offcuts(self):
        self.ensure_one()
        return self._tp_get_offcut_action([("company_id", "=", self.company_id.id)])

    def action_open_available_offcuts(self):
        self.ensure_one()
        return self._tp_get_offcut_action(
            [("company_id", "=", self.company_id.id), ("state", "=", "available")]
        )

    def action_open_reserved_offcuts(self):
        self.ensure_one()
        return self._tp_get_offcut_action(
            [("company_id", "=", self.company_id.id), ("state", "=", "reserved")]
        )

    def action_open_in_use_offcuts(self):
        self.ensure_one()
        return self._tp_get_offcut_action([("company_id", "=", self.company_id.id), ("state", "=", "in_use")])

    def action_open_sold_offcuts(self):
        self.ensure_one()
        return self._tp_get_offcut_action([("company_id", "=", self.company_id.id), ("state", "=", "sold")])

    def action_open_inactive_offcuts(self):
        self.ensure_one()
        return self._tp_get_offcut_action(
            [("company_id", "=", self.company_id.id), ("state", "=", "inactive")]
        )

    def action_open_cleanup_due_offcuts(self):
        self.ensure_one()
        cutoff = fields.Datetime.now() - timedelta(days=max(int(self.company_id.tp_offcut_sold_cleanup_days or 30), 1))
        domain = [
            ("company_id", "=", self.company_id.id),
            ("state", "=", "sold"),
            ("sold_at", "!=", False),
            ("sold_at", "<=", cutoff),
        ]
        sold_bin = self.company_id.tp_offcut_sold_bin_location_id
        if sold_bin:
            domain.append(("bin_location_id", "=", sold_bin.id))
        return self._tp_get_offcut_action(domain)

    def action_open_waste_last_30d(self):
        self.ensure_one()
        cutoff = fields.Datetime.now() - timedelta(days=30)
        action = self.env.ref("tp_offcuts_nesting.action_tp_offcut_waste").read()[0]
        action["domain"] = [
            ("company_id", "=", self.company_id.id),
            ("timestamp", ">=", cutoff),
        ]
        action["context"] = dict(self.env.context)
        return action

    def action_open_reserved_mos(self):
        self.ensure_one()
        reserved_mo_ids = self.env["tp.offcut"].with_context(active_test=False).search(
            [
                ("company_id", "=", self.company_id.id),
                ("state", "=", "reserved"),
                ("reserved_mo_id", "!=", False),
            ]
        ).mapped("reserved_mo_id").ids
        return {
            "type": "ir.actions.act_window",
            "name": "Reserved Manufacturing Orders",
            "res_model": "mrp.production",
            "view_mode": "list,form",
            "domain": [("id", "in", reserved_mo_ids)],
            "target": "current",
        }

    @api.model
    def _tp_get_or_create_company_dashboard(self):
        company = self.env.company
        dashboard = self.search([("company_id", "=", company.id)], limit=1)
        if dashboard:
            return dashboard
        try:
            return self.create({"company_id": company.id})
        except ValidationError:
            dashboard = self.search([("company_id", "=", company.id)], limit=1)
            if dashboard:
                return dashboard
            raise

    @api.model
    def action_open_current_company_dashboard(self):
        dashboard = self._tp_get_or_create_company_dashboard()
        return {
            "type": "ir.actions.act_window",
            "name": "Operations Dashboard",
            "res_model": "tp.offcut.operational.dashboard",
            "view_mode": "form",
            "view_id": self.env.ref("tp_offcuts_nesting.view_tp_offcut_operational_dashboard_form").id,
            "res_id": dashboard.id,
            "target": "current",
        }

    def action_refresh(self):
        self.ensure_one()
        self.write({"as_of": fields.Datetime.now()})
        return {"type": "ir.actions.client", "tag": "reload"}
