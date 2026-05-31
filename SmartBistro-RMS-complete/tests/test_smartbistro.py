import tempfile
import unittest
from pathlib import Path

from src.app import ApiError, SmartBistroService, verify_token


class SmartBistroFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.service = SmartBistroService(Path(self.tmpdir.name) / "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_login_returns_signed_role_token(self):
        result = self.service.login("manager@smartbistro.test", "manager123")
        payload = verify_token(result["token"])
        self.assertEqual(payload["role"], "manager")
        self.assertEqual(result["user"]["email"], "manager@smartbistro.test")

    def test_menu_for_table_contains_seeded_categories(self):
        menu = self.service.menu_for_table(1)
        self.assertEqual(menu["table"]["label"], "T1")
        self.assertIn("Mains", menu["categories"])
        self.assertGreaterEqual(len(menu["items"]), 6)

    def test_order_deducts_stock_creates_kds_card_and_loyalty(self):
        before = self.service.inventory()["ingredients"]
        pasta_before = next(item for item in before if item["name"] == "Pasta")["stock"]
        order = self.service.create_order(
            {
                "table_id": 2,
                "items": [{"menu_item_id": 2, "qty": 2}],
                "customer": {"name": "Test Guest", "contact": "guest@example.com"},
                "payment_method": "table-card",
            }
        )
        self.assertEqual(order["status"], "received")
        self.assertEqual(order["payment_status"], "paid")
        self.assertTrue(any(kds["id"] == order["id"] for kds in self.service.kds_orders()))
        after = self.service.inventory()["ingredients"]
        pasta_after = next(item for item in after if item["name"] == "Pasta")["stock"]
        self.assertLess(pasta_after, pasta_before)
        loyalty = self.service.customer_loyalty("guest@example.com")
        self.assertEqual(loyalty["loyalty_points"], 45)
        self.assertEqual(order["loyalty"]["loyalty_points"], 45)

    def test_insufficient_stock_blocks_order_atomically(self):
        with self.service.connect() as conn:
            conn.execute("UPDATE ingredients SET stock = 0 WHERE name = 'Beef patty'")
        with self.assertRaises(ApiError) as raised:
            self.service.create_order({"table_id": 1, "items": [{"menu_item_id": 3, "qty": 1}]})
        self.assertEqual(raised.exception.status, 409)

    def test_order_status_served_marks_table_dirty(self):
        order = self.service.create_order({"table_id": 4, "items": [{"menu_item_id": 6, "qty": 1}]})
        updated = self.service.update_order_status(order["id"], "served")
        table = next(item for item in self.service.tables() if item["id"] == 4)
        self.assertEqual(updated["status"], "served")
        self.assertEqual(table["status"], "dirty")

    def test_role_guard_rejects_guest_access_to_manager_dashboard(self):
        with self.assertRaises(ApiError) as raised:
            self.service.require_user(None, ("manager",))
        self.assertEqual(raised.exception.status, 401)

    def test_analytics_summarises_paid_orders(self):
        self.service.create_order({"table_id": 5, "items": [{"menu_item_id": 1, "qty": 1}]})
        dashboard = self.service.analytics_dashboard()
        self.assertEqual(dashboard["summary"]["orders"], 1)
        self.assertGreater(dashboard["summary"]["revenue"], 0)
        self.assertEqual(dashboard["top_dishes"][0]["name"], "Margherita Pizza")

    def test_order_history_lists_completed_service_records(self):
        order = self.service.create_order({"table_id": 8, "items": [{"menu_item_id": 6, "qty": 2}]})
        history = self.service.order_history()
        self.assertEqual(history["orders"][0]["id"], order["id"])
        self.assertEqual(history["orders"][0]["items"][0]["name"], "Espresso")

    def test_manager_can_create_and_edit_menu_item(self):
        item = self.service.save_menu_item(
            {
                "name": "Soup of the Day",
                "category": "Specials",
                "description": "Chef's rotating soup.",
                "price": 11.5,
                "prep_minutes": 8,
                "allergens_text": "",
                "dietary_text": "vegetarian",
                "image_url": "/assets/salad.png",
            }
        )
        self.assertEqual(item["name"], "Soup of the Day")
        self.assertEqual(item["price"], 11.5)
        updated = self.service.save_menu_item({**item, "price": 12.0, "dietary_text": "vegetarian"}, item["id"])
        self.assertEqual(updated["price"], 12.0)

    def test_table_qr_is_svg(self):
        svg = self.service.table_qr_svg(1, "http://localhost:8000")
        self.assertIn("<svg", svg)
        self.assertIn("rect", svg)


if __name__ == "__main__":
    unittest.main()
