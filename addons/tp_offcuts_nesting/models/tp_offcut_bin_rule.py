from odoo import api, fields, models


class TpOffcutBinRule(models.Model):
    _name = "tp.offcut.bin.rule"
    _description = "TP Offcut BIN Rule"
    _order = "sequence, id"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    # --- Thickness matching ---------------------------------------------------
    # Comma-separated list of thicknesses (mm) this rule applies to, e.g.
    # "2,3" or "8,10". Blank = any thickness. Matched with a small tolerance so
    # 3.0 matches "3".
    thicknesses_mm = fields.Char(
        string="Thicknesses (mm)",
        help="Comma-separated thicknesses this rule covers, e.g. '2,3'. "
             "Leave blank to match any thickness.",
    )

    # --- Size matching --------------------------------------------------------
    # Minimum dimensions (kept for backwards compatibility / fine control).
    min_width_mm = fields.Integer(default=0, required=True)
    min_height_mm = fields.Integer(default=0, required=True)
    # Maximum envelope. An offcut is "within" the envelope if it fits inside
    # max_width x max_height in EITHER orientation (so min side <= max_width and
    # max side <= max_height). 0 = no maximum on that dimension.
    max_width_mm = fields.Integer(
        default=0,
        help="Max envelope width (mm). 0 = no limit. Tested in either "
             "orientation: the offcut's shorter side must be <= this.",
    )
    max_height_mm = fields.Integer(
        default=0,
        help="Max envelope height (mm). 0 = no limit. Tested in either "
             "orientation: the offcut's longer side must be <= this.",
    )

    bin_location_id = fields.Many2one(
        "stock.location",
        required=True,
        domain="[('usage', '=', 'internal')]",
    )

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------
    def _thickness_set(self):
        """Parsed set of int-rounded thicknesses this rule covers (empty = any)."""
        self.ensure_one()
        out = set()
        for tok in (self.thicknesses_mm or "").replace(";", ",").split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                out.add(round(float(tok), 1))
            except ValueError:
                continue
        return out

    def _matches_offcut(self, *, width_mm, height_mm, thickness_mm):
        """True if this rule applies to an offcut of these dimensions/thickness."""
        self.ensure_one()
        # Thickness.
        wanted = self._thickness_set()
        if wanted:
            t = round(float(thickness_mm or 0.0), 1)
            if not any(abs(t - w) <= 0.25 for w in wanted):
                return False
        w = int(width_mm or 0)
        h = int(height_mm or 0)
        # Minimum dimensions (orientation as stored).
        if w < self.min_width_mm or h < self.min_height_mm:
            return False
        # Maximum envelope, orientation-independent: shorter side <= max_width,
        # longer side <= max_height.
        if self.max_width_mm or self.max_height_mm:
            short, long_ = (w, h) if w <= h else (h, w)
            if self.max_width_mm and short > self.max_width_mm:
                return False
            if self.max_height_mm and long_ > self.max_height_mm:
                return False
        return True

    def action_reapply_bin_rules(self):
        """Re-bin existing offcuts against the current rules. Skips sold/inactive
        offcuts (those were deliberately placed, e.g. in the sold bin)."""
        offcuts = self.env["tp.offcut"].sudo().search([
            ("active", "=", True),
            ("state", "not in", ("sold", "inactive")),
        ])
        offcuts.write({"bin_location_id": False})
        offcuts._assign_bin_location_from_rules()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "BIN Rules re-applied",
                "message": "Re-binned %d offcut(s) using the current rules." % len(offcuts),
                "type": "success",
                "sticky": False,
            },
        }
