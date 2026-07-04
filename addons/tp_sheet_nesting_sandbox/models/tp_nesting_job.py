from odoo import models


class TpNestingJob(models.Model):
    _inherit = "tp.nesting.job"

    def action_open_nesting_sandbox(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Preview Nest",
            "res_model": "tp.nesting.sandbox",
            "view_mode": "form",
            "target": "new",
            "context": {
                "active_id": self.id,
                "active_model": "tp.nesting.job",
            },
        }
