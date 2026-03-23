from odoo import models
from odoo.exceptions import UserError


class MrpUnbuild(models.Model):
    _inherit = "mrp.unbuild"

    @staticmethod
    def _tp_is_nesting_managed_mo(mo):
        if not mo:
            return False
        if "tp_last_nesting_run_id" in mo._fields and mo.tp_last_nesting_run_id:
            return True
        if "x_tp_source_so_line_id" in mo._fields and mo.x_tp_source_so_line_id:
            return True
        return "tp_cut_line_ids" in mo._fields and bool(mo.tp_cut_line_ids)

    def action_unbuild(self):
        if self.env.context.get("tp_allow_nesting_unbuild"):
            return super().action_unbuild()
        blocked = self.filtered(lambda rec: self._tp_is_nesting_managed_mo(rec.mo_id))
        if blocked:
            labels = ", ".join(blocked.mapped("name"))
            raise UserError(
                "Unbuild is blocked for nesting-managed manufacturing orders. "
                "Use inventory adjustment/scrap workflows instead. "
                f"Blocked unbuild order(s): {labels}"
            )
        return super().action_unbuild()
