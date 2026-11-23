"""Tests for refund_service. Run: python -m unittest -v"""

import json
import os
import tempfile
import unittest
from datetime import timedelta

import refund_service as rs


class RefundServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = rs.DB_PATH
        rs.DB_PATH = os.path.join(self._tmp.name, "store_db.json")
        rs.reset_database()

    def tearDown(self):
        rs.DB_PATH = self._orig_path
        self._tmp.cleanup()

    def _db(self):
        with open(rs.DB_PATH, encoding="utf-8") as f:
            return json.load(f)

    # --- policy -----------------------------------------------------------

    def test_policy_verdicts_for_mock_orders(self):
        self.assertTrue(rs.check_refund_policy("ORD-101", "changed_mind")["eligible"])
        for oid, fragment in [
            ("ORD-102", "Refund window expired"),
            ("ORD-103", "non-refundable"),
            ("ORD-104", "already been refunded"),
            ("ORD-105", "in transit"),
        ]:
            policy = rs.check_refund_policy(oid, "defective")
            self.assertFalse(policy["eligible"], oid)
            self.assertIn(fragment, " ".join(policy["violations"]), oid)

    def test_unknown_order(self):
        with self.assertRaises(rs.OrderNotFoundError):
            rs.get_order_details("ORD-999")

    # --- amount + quote ---------------------------------------------------

    def test_changed_mind_withholds_ten_percent(self):
        quote = rs.calculate_refund_amount("ORD-101", "changed mind")
        self.assertEqual(quote["refund_amount"], 225.0)
        self.assertEqual(quote["fee_amount"], 25.0)
        self.assertIn(quote["quote_id"], self._db()["quotes"])

    # --- execution --------------------------------------------------------

    def test_happy_path(self):
        quote = rs.calculate_refund_amount("ORD-101", "changed_mind")
        result = rs.execute_refund_transaction("ORD-101", quote["quote_id"])
        self.assertEqual(result["amount"], 225.0)
        receipt = rs.send_refund_notification("ORD-101", result["refund_id"], 225.0)
        self.assertEqual(receipt["recipient"], "alice@example.com")
        self.assertEqual(rs.get_order_details("ORD-101")["status"], "refunded")

    def test_unknown_quote_rejected(self):
        with self.assertRaises(rs.RefundNotAllowedError):
            rs.execute_refund_transaction("ORD-101", "Q-ORD-101-FAKE")
        self.assertEqual(rs.get_order_details("ORD-101")["status"], "delivered")

    def test_quote_is_single_use(self):
        quote = rs.calculate_refund_amount("ORD-101", "defective")
        rs.execute_refund_transaction("ORD-101", quote["quote_id"])
        with self.assertRaises(rs.RefundNotAllowedError):
            rs.execute_refund_transaction("ORD-101", quote["quote_id"])

    def test_quote_bound_to_order(self):
        quote = rs.calculate_refund_amount("ORD-101", "defective")
        with self.assertRaises(rs.RefundNotAllowedError):
            rs.execute_refund_transaction("ORD-102", quote["quote_id"])

    def test_expired_quote_rejected(self):
        quote = rs.calculate_refund_amount("ORD-101", "defective")
        db = self._db()
        db["quotes"][quote["quote_id"]]["expires_at"] = rs._iso(rs._now() - timedelta(seconds=1))
        rs._save_db(db)
        with self.assertRaises(rs.RefundNotAllowedError):
            rs.execute_refund_transaction("ORD-101", quote["quote_id"])

    def test_execution_rechecks_policy(self):
        # A quote can be issued for any order, but executing it re-runs the policy.
        for oid in ["ORD-102", "ORD-103", "ORD-105"]:
            quote = rs.calculate_refund_amount(oid, "defective")
            with self.assertRaises(rs.RefundNotAllowedError, msg=oid):
                rs.execute_refund_transaction(oid, quote["quote_id"])
        self.assertEqual(len(self._db()["transactions"]), 1)  # only the ORD-104 seed

    # --- notification -----------------------------------------------------

    def test_notification_requires_matching_refund(self):
        with self.assertRaises(rs.RefundNotAllowedError):
            rs.send_refund_notification("ORD-101", "RF-ORD-101-FAKE", 250.0)
        with self.assertRaises(rs.RefundNotAllowedError):
            rs.send_refund_notification("ORD-104", "RF-ORD-104-SEED", 1.0)
        with self.assertRaises(rs.RefundNotAllowedError):
            rs.send_refund_notification("ORD-104", "RF-ORD-104-SEED", "180")


if __name__ == "__main__":
    unittest.main()
