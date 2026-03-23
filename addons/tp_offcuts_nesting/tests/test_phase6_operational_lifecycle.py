from datetime import timedelta

from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("phase6")
class TestPhase6OperationalLifecycle(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.product = cls.env["product.product"].create(
            {
                "name": "Phase 6 Offcut Product",
                "type": "consu",
                "tracking": "lot",
            }
        )
        cls.sold_bin = cls.env["stock.location"].create(
            {
                "name": "Sold BIN P6",
                "usage": "internal",
                "location_id": cls.env.ref("stock.stock_location_stock").id,
            }
        )
        cls.company.tp_offcut_sold_bin_location_id = cls.sold_bin
        cls.company.tp_offcut_sold_cleanup_days = 30

    @classmethod
    def _new_lot(cls, name):
        return cls.env["stock.lot"].create(
            {
                "name": name,
                "product_id": cls.product.id,
                "company_id": cls.company.id,
            }
        )

    def _new_offcut(self, name, *, value=0.0):
        parent_lot = self._new_lot(f"{name}-PARENT")
        return self.env["tp.offcut"].create(
            {
                "name": name,
                "lot_id": self._new_lot(f"{name}-LOT").id,
                "width_mm": 800,
                "height_mm": 600,
                "source_type": "sheet",
                "parent_lot_id": parent_lot.id,
                "remaining_value": value,
            }
        )

    def test_sold_bin_cleanup_cron_removes_old_offcuts(self):
        old_offcut = self._new_offcut("P6-OLD-SOLD")
        old_lot = old_offcut.lot_id
        old_offcut.action_set_sold()
        old_offcut.write({"sold_at": fields.Datetime.now() - timedelta(days=31)})

        recent_offcut = self._new_offcut("P6-RECENT-SOLD")
        recent_lot = recent_offcut.lot_id
        recent_offcut.action_set_sold()
        recent_offcut.write({"sold_at": fields.Datetime.now() - timedelta(days=5)})

        result = self.env["tp.offcut"].cron_cleanup_sold_offcuts()

        self.assertGreaterEqual(result["removed_offcuts"], 1)
        self.assertFalse(old_offcut.exists())
        self.assertFalse(old_lot.exists())
        self.assertTrue(recent_offcut.exists())
        self.assertTrue(recent_lot.exists())

    def test_operational_dashboard_matches_underlying_metrics(self):
        available = self._new_offcut("P6-AVAILABLE", value=10.0)
        reserved = self._new_offcut("P6-RESERVED", value=20.0)
        reserved.action_set_reserved(False)
        in_use = self._new_offcut("P6-IN-USE", value=30.0)
        in_use.action_set_in_use()
        sold_due = self._new_offcut("P6-SOLD-DUE", value=40.0)
        sold_due.action_set_sold()
        sold_due.write({"sold_at": fields.Datetime.now() - timedelta(days=31)})
        inactive = self._new_offcut("P6-INACTIVE", value=50.0)
        inactive.action_archive()

        dashboard = self.env["tp.offcut.operational.dashboard"].search(
            [("company_id", "=", self.company.id)],
            limit=1,
        )
        if not dashboard:
            dashboard = self.env["tp.offcut.operational.dashboard"].create({"company_id": self.company.id})
        dashboard.action_refresh()

        self.assertGreaterEqual(dashboard.total_offcut_count, 5)
        self.assertGreaterEqual(dashboard.available_offcut_count, 1)
        self.assertGreaterEqual(dashboard.reserved_offcut_count, 1)
        self.assertGreaterEqual(dashboard.in_use_offcut_count, 1)
        self.assertGreaterEqual(dashboard.sold_offcut_count, 1)
        self.assertGreaterEqual(dashboard.inactive_offcut_count, 1)
        self.assertGreaterEqual(dashboard.sold_cleanup_due_count, 1)
        self.assertGreaterEqual(dashboard.inventory_value_total, 60.0)
