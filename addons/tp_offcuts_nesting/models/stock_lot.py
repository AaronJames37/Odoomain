from odoo import api, fields, models


class StockLot(models.Model):
    _inherit = "stock.lot"

    tp_width_mm = fields.Integer(string="Width (mm)")
    tp_height_mm = fields.Integer(string="Height (mm)")
    tp_is_offcut = fields.Boolean(string="Is Offcut", default=False)
    tp_lot_type = fields.Selection(
        [("offcut", "Offcuts"), ("sheet", "Full Sheets")],
        string="Lot Type",
        compute="_compute_tp_lot_type",
        store=True,
        index=True,
        readonly=True,
    )
    tp_parent_lot_id = fields.Many2one(
        "stock.lot",
        string="Parent Lot",
        ondelete="set null",
    )

    @api.depends("tp_is_offcut")
    def _compute_tp_lot_type(self):
        for lot in self:
            lot.tp_lot_type = "offcut" if lot.tp_is_offcut else "sheet"
