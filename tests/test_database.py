import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from database import PayPulseDatabase, VaultError, hash_password, paystub_signature
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

    def unlock(self, username, password):
        unlocked = self.database.unlock_user(username, password)
        self.assertIsNotNone(unlocked)
        return unlocked

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
        _, first_key, first_recovery = self.unlock("first", "first secure password")
        token, csrf = self.database.create_session(first["id"], first_key, first_recovery)
        session = self.database.session_user(token)

        self.assertEqual(session[0]["id"], first["id"])
        self.assertEqual(session[1], csrf)
        self.assertEqual(session[2], first_key)

        planner = normalize_planner(
            {
                "allocations": {"mode": "percent", "values": {"needs": 40, "savings": 30, "debt": 20, "flexible": 10}},
                "expenses": [],
                "goals": [],
            }
        )
        _, second_key, _ = self.unlock("second", "second secure password")
        self.database.set_planner(first["id"], planner, first_key)
        self.assertTrue(self.database.get_planner(first["id"], DEFAULT_PLANNER, first_key)[0])
        self.assertFalse(self.database.get_planner(second["id"], DEFAULT_PLANNER, second_key)[0])

    def test_paystubs_are_duplicate_safe_and_user_scoped(self):
        first = self.database.register_user("first", "first secure password")
        second = self.database.register_user("second", "second secure password")
        statement = ParsedStatement(sample_record(), {"status": "OK"}, 1)
        _, first_key, _ = self.unlock("first", "first secure password")
        _, second_key, _ = self.unlock("second", "second secure password")

        first_result = self.database.add_statements(first["id"], [statement, statement], first_key)
        second_result = self.database.add_statements(second["id"], [statement], second_key)

        self.assertEqual(first_result["added"], 1)
        self.assertEqual(first_result["duplicates"], 1)
        self.assertEqual(second_result["added"], 1)
        self.assertEqual(len(self.database.list_paystubs(first["id"], first_key)), 1)
        self.assertEqual(len(self.database.list_paystubs(second["id"], second_key)), 1)

    def test_imported_paystub_records_survive_database_reopen(self):
        user = self.database.register_user("payrolluser", "persistent secure password")
        _, vault_key, _ = self.unlock("payrolluser", "persistent secure password")
        result = self.database.add_paystub_records(
            user["id"], [sample_record(), sample_record()], vault_key
        )

        reopened = PayPulseDatabase(self.database.path)
        reopened.initialize()
        reopened_user, reopened_key, _ = reopened.unlock_user(
            "payrolluser", "persistent secure password"
        )
        stored = reopened.list_paystubs(reopened_user["id"], reopened_key)

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["total_records"], 1)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["overtime_hours"], 0.8)

    def test_paystub_records_can_be_updated_and_deleted_only_by_their_owner(self):
        owner = self.database.register_user("owner", "correct horse battery staple")
        other = self.database.register_user("other", "another secure password")
        _, owner_key, _ = self.unlock("owner", "correct horse battery staple")
        _, other_key, _ = self.unlock("other", "another secure password")
        self.database.add_paystub_records(
            owner["id"], [sample_record(), sample_record("2026-08-07")], owner_key
        )
        stored = self.database.list_paystubs(owner["id"], owner_key)
        record_id = stored[0]["_record_id"]
        edited = {**stored[0], "gross_pay": 900, "net_pay": 748.98}

        updated = self.database.update_paystub_record(owner["id"], record_id, edited, owner_key)

        self.assertEqual(updated["_record_id"], record_id)
        self.assertEqual(updated["gross_pay"], 900)
        self.assertEqual(updated["calculated_net"], 748.98)
        with self.assertRaisesRegex(LookupError, "not found"):
            self.database.update_paystub_record(other["id"], record_id, edited, other_key)
        with self.assertRaisesRegex(LookupError, "not found"):
            self.database.delete_paystub_record(other["id"], record_id)

        duplicate = {**self.database.list_paystubs(owner["id"], owner_key)[0]}
        second_id = self.database.list_paystubs(owner["id"], owner_key)[1]["_record_id"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.database.update_paystub_record(owner["id"], second_id, duplicate, owner_key)

        self.database.delete_paystub_record(owner["id"], record_id)
        self.assertEqual(len(self.database.list_paystubs(owner["id"], owner_key)), 1)

    def test_last_active_admin_cannot_be_removed(self):
        admin = self.database.register_user("owner", "correct horse battery staple")
        with self.assertRaisesRegex(ValueError, "owner"):
            self.database.update_user(admin["id"], role="member")
        with self.assertRaisesRegex(ValueError, "owner"):
            self.database.delete_user(admin["id"])

    def test_raw_database_contains_only_encrypted_financial_payloads(self):
        user = self.database.register_user("owner", "correct horse battery staple")
        _, vault_key, _ = self.unlock("owner", "correct horse battery staple")
        record = sample_record()
        planner = normalize_planner(
            {
                "allocations": DEFAULT_PLANNER["allocations"],
                "expenses": [{"id": "rent", "name": "Secret Rent", "amount": 900, "frequency": "monthly"}],
                "goals": [{"id": "trip", "name": "Secret Trip", "target": 2000, "saved": 50, "date": "2027-01-01"}],
            }
        )
        self.database.add_paystub_records(user["id"], [record], vault_key)
        self.database.set_planner(user["id"], planner, vault_key)

        with self.database.connect() as connection:
            paystub = connection.execute(
                "SELECT signature, pay_date, payload FROM paystubs WHERE user_id = ?", (user["id"],)
            ).fetchone()
            stored_planner = connection.execute(
                "SELECT payload FROM planners WHERE user_id = ?", (user["id"],)
            ).fetchone()[0]
        raw = self.database.path.read_bytes()
        self.assertEqual(paystub["pay_date"], "")
        self.assertNotEqual(paystub["signature"], paystub_signature(record))
        for marker in (b"2026-07-24", b"894.04", b"Hourly", b"Secret Rent", b"Secret Trip"):
            self.assertNotIn(marker, raw)
        self.assertIn('"alg":"A256GCM"', paystub["payload"])
        self.assertIn('"alg":"A256GCM"', stored_planner)

    def test_ciphertext_tampering_and_cross_user_copy_are_rejected(self):
        first = self.database.register_user("first", "first secure password")
        second = self.database.register_user("second", "second secure password")
        _, first_key, _ = self.unlock("first", "first secure password")
        _, second_key, _ = self.unlock("second", "second secure password")
        self.database.add_paystub_records(first["id"], [sample_record()], first_key)
        with self.database.connect() as connection:
            payload = connection.execute(
                "SELECT payload FROM paystubs WHERE user_id = ?", (first["id"],)
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO paystubs(user_id, signature, pay_date, payload, created_at) VALUES (?, 'copied', '', ?, 1)",
                (second["id"], payload),
            )
        with self.assertRaises(VaultError):
            self.database.list_paystubs(second["id"], second_key)

    def test_password_change_and_owner_recovery_rewrap_without_changing_ciphertext(self):
        owner = self.database.register_user("owner", "correct horse battery staple")
        member = self.database.register_user("member", "member secure password")
        _, owner_key, recovery_private = self.unlock("owner", "correct horse battery staple")
        _, member_key, _ = self.unlock("member", "member secure password")
        self.database.add_paystub_records(member["id"], [sample_record()], member_key)
        with self.database.connect() as connection:
            before = connection.execute(
                "SELECT payload FROM paystubs WHERE user_id = ?", (member["id"],)
            ).fetchone()[0]

        reset = self.database.reset_password(
            owner["id"], member["id"], "temporary secure password", recovery_private
        )
        self.assertTrue(reset["must_change_password"])
        self.assertIsNone(self.database.unlock_user("member", "member secure password"))
        unlocked_member, recovered_key, _ = self.unlock("member", "temporary secure password")
        self.assertEqual(len(self.database.list_paystubs(member["id"], recovered_key)), 1)
        self.database.change_password(
            member["id"],
            "temporary secure password",
            "new permanent secure password",
            recovered_key,
        )
        changed, changed_key, _ = self.unlock("member", "new permanent secure password")
        self.assertFalse(changed["must_change_password"])
        self.assertEqual(changed_key, recovered_key)
        with self.database.connect() as connection:
            after = connection.execute(
                "SELECT payload FROM paystubs WHERE user_id = ?", (member["id"],)
            ).fetchone()[0]
        self.assertEqual(before, after)

    def test_initialize_invalidates_sessions_and_in_memory_keys(self):
        user = self.database.register_user("owner", "correct horse battery staple")
        _, vault_key, recovery_private = self.unlock("owner", "correct horse battery staple")
        token, _ = self.database.create_session(user["id"], vault_key, recovery_private)
        self.assertIsNotNone(self.database.session_user(token))
        self.database.initialize()
        self.assertIsNone(self.database.session_user(token))

    def test_legacy_plaintext_migrates_transactionally_and_scrubs_sqlite_pages(self):
        legacy_path = Path(self.temporary.name) / "legacy.db"
        password = "legacy secure password"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    csrf_token TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE TABLE planners (user_id INTEGER PRIMARY KEY, payload TEXT NOT NULL, updated_at INTEGER NOT NULL);
                CREATE TABLE paystubs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    signature TEXT NOT NULL, pay_date TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL, UNIQUE(user_id, signature)
                );
                """
            )
            now = int(time.time())
            connection.execute(
                "INSERT INTO users(username, password_hash, role, active, created_at, updated_at) VALUES ('legacy', ?, 'admin', 1, ?, ?)",
                (hash_password(password), now, now),
            )
            record = sample_record()
            connection.execute(
                "INSERT INTO paystubs(user_id, signature, pay_date, payload, created_at) VALUES (1, ?, ?, ?, ?)",
                (paystub_signature(record), record["pay_date"], json.dumps(record), now),
            )
            connection.execute(
                "INSERT INTO planners(user_id, payload, updated_at) VALUES (1, ?, ?)",
                (json.dumps({"allocations": DEFAULT_PLANNER["allocations"], "expenses": [], "goals": []}), now),
            )
            connection.commit()
        finally:
            connection.close()

        legacy = PayPulseDatabase(legacy_path)
        legacy.initialize()
        user, vault_key, recovery_private = legacy.unlock_user("legacy", password)
        self.assertTrue(user["is_owner"])
        self.assertTrue(user["encryption_migrated"])
        self.assertIsNotNone(recovery_private)
        self.assertEqual(len(legacy.list_paystubs(user["id"], vault_key)), 1)
        raw = legacy_path.read_bytes()
        self.assertNotIn(b"2026-07-24", raw)
        self.assertNotIn(b"894.04", raw)
        with legacy.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT pay_date FROM paystubs").fetchone()[0], ""
            )

    def test_failed_legacy_migration_rolls_back_without_removing_plaintext(self):
        self.database.register_user("owner", "correct horse battery staple")
        now = int(time.time())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users(username, password_hash, role, active, created_at, updated_at)
                VALUES ('legacy-member', ?, 'member', 1, ?, ?)
                """,
                (hash_password("legacy member password"), now, now),
            )
            member_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO planners(user_id, payload, updated_at) VALUES (?, 'private broken planner', ?)",
                (member_id, now),
            )

        with self.assertRaises(json.JSONDecodeError):
            self.database.unlock_user("legacy-member", "legacy member password")
        with self.database.connect() as connection:
            user = connection.execute(
                "SELECT crypto_version, wrapped_vault_key FROM users WHERE id = ?", (member_id,)
            ).fetchone()
            planner = connection.execute(
                "SELECT payload FROM planners WHERE user_id = ?", (member_id,)
            ).fetchone()[0]
        self.assertEqual(user["crypto_version"], 0)
        self.assertIsNone(user["wrapped_vault_key"])
        self.assertEqual(planner, "private broken planner")


if __name__ == "__main__":
    unittest.main()
