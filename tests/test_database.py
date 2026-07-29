import tempfile
import unittest
from pathlib import Path

from database import PayPulseDatabase
from ingestion import ParsedStatement
from server import DEFAULT_PLANNER, normalize_planner


def sample_record(pay_date="2026-07-24"):
    return {
        "pay_date": pay_date,
        "period_begin": "2026-07-06",
        "period_end": "2026-07-19",
        "year": 2026,
        "pay_type": "Hourly",
        "payment_type": "Payroll",
        "gross_pay": 894.04,
        "total_taxes": 131.02,
        "total_deductions": 20,
        "calculated_net": 743.02,
        "net_pay": 743.02,
        "hours_units": 79.07,
        "regular_rate": 11.25,
        "regular_hours": 78.27,
        "regular_pay": 880.54,
        "overtime_rate": 16.875,
        "overtime_hours": 0.8,
        "overtime_pay": 13.5,
        "bonus_pay": 0,
        "reported_tips": 85.82,
        "social_security_tax": 60.75,
        "medicare_tax": 14.21,
        "federal_withholding": 36.06,
        "mississippi_withholding": 20,
        "roth_401k": 20,
        "dental_insurance": 0,
        "health_insurance": 0,
    }


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = PayPulseDatabase(Path(self.temporary.name) / "paypulse.db")
        self.database.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def test_first_account_is_admin_and_authentication_is_hashed(self):
        admin = self.database.register_user("owner", "correct horse battery staple")
        member = self.database.register_user("member", "another long password")

        self.assertEqual(admin["role"], "admin")
        self.assertEqual(member["role"], "member")
        self.assertIsNone(self.database.authenticate("owner", "wrong password"))
        self.assertEqual(self.database.authenticate("OWNER", "correct horse battery staple")["id"], admin["id"])

        with self.database.connect() as connection:
            stored_hash = connection.execute(
                "SELECT password_hash FROM users WHERE id = ?", (admin["id"],)
            ).fetchone()[0]
        self.assertNotIn("correct horse", stored_hash)
        self.assertTrue(stored_hash.startswith("pbkdf2_sha256$"))

    def test_sessions_and_planners_are_isolated_by_user(self):
        first = self.database.register_user("first", "first secure password")
        second = self.database.register_user("second", "second secure password")
        token, csrf = self.database.create_session(first["id"])
        session = self.database.session_user(token)

        self.assertEqual(session[0]["id"], first["id"])
        self.assertEqual(session[1], csrf)

        planner = normalize_planner(
            {
                "allocations": {"mode": "percent", "values": {"needs": 40, "savings": 30, "debt": 20, "flexible": 10}},
                "expenses": [],
                "goals": [],
            }
        )
        self.database.set_planner(first["id"], planner)
        self.assertTrue(self.database.get_planner(first["id"], DEFAULT_PLANNER)[0])
        self.assertFalse(self.database.get_planner(second["id"], DEFAULT_PLANNER)[0])

    def test_paystubs_are_duplicate_safe_and_user_scoped(self):
        first = self.database.register_user("first", "first secure password")
        second = self.database.register_user("second", "second secure password")
        statement = ParsedStatement(sample_record(), {"status": "OK"}, 1)

        first_result = self.database.add_statements(first["id"], [statement, statement])
        second_result = self.database.add_statements(second["id"], [statement])

        self.assertEqual(first_result["added"], 1)
        self.assertEqual(first_result["duplicates"], 1)
        self.assertEqual(second_result["added"], 1)
        self.assertEqual(len(self.database.list_paystubs(first["id"])), 1)
        self.assertEqual(len(self.database.list_paystubs(second["id"])), 1)

    def test_imported_paystub_records_survive_database_reopen(self):
        user = self.database.register_user("payrolluser", "persistent secure password")
        result = self.database.add_paystub_records(
            user["id"], [sample_record(), sample_record()]
        )

        reopened = PayPulseDatabase(self.database.path)
        reopened.initialize()
        stored = reopened.list_paystubs(user["id"])

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["total_records"], 1)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["overtime_hours"], 0.8)

    def test_last_active_admin_cannot_be_removed(self):
        admin = self.database.register_user("owner", "correct horse battery staple")
        with self.assertRaisesRegex(ValueError, "administrator"):
            self.database.update_user(admin["id"], role="member")
        with self.assertRaisesRegex(ValueError, "administrator"):
            self.database.delete_user(admin["id"])


if __name__ == "__main__":
    unittest.main()
