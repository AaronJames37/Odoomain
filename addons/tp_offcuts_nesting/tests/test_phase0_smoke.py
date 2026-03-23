from odoo.tests.common import TransactionCase


class TestPhase0Smoke(TransactionCase):
    def test_placeholder_model_available(self):
        record = self.env["tp.offcuts.placeholder"].create({"name": "Smoke"})
        self.assertTrue(record.id)
