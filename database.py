"""SQLite persistence and authentication for PayPulse."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from ingestion import CSV_FIELDS, ParsedStatement


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
PASSWORD_ITERATIONS = 310_000
SESSION_SECONDS = 7 * 24 * 60 * 60
TEXT_PAYSTUB_FIELDS = {"pay_date", "period_begin", "period_end", "pay_type", "payment_type"}


class ClosingConnection(sqlite3.Connection):
    """Commit or roll back a context block, then release the SQLite file handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def validate_username(username: object) -> str:
    value = str(username or "").strip()
    if not USERNAME_PATTERN.fullmatch(value):
        raise ValueError(
            "Username must be 3–32 characters and use only letters, numbers, dots, dashes, or underscores."
        )
    return value


def validate_password(password: object) -> str:
    value = str(password or "")
    if len(value) < 10:
        raise ValueError("Password must contain at least 10 characters.")
    if len(value) > 128:
        raise ValueError("Password must contain no more than 128 characters.")
    return value


def hash_password(password: str, *, iterations: int = PASSWORD_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            base64.urlsafe_b64decode(salt.encode("ascii")),
            int(iterations),
        )
        return hmac.compare_digest(
            base64.urlsafe_b64encode(digest).decode("ascii"), expected
        )
    except (TypeError, ValueError):
        return False


def normalize_paystub_record(record: dict[str, object]) -> dict[str, object]:
    if not isinstance(record, dict):
        raise ValueError("Every payroll statement must be a JSON object.")
    normalized: dict[str, object] = {}
    for field in CSV_FIELDS:
        value = record.get(field, "")
        if field in TEXT_PAYSTUB_FIELDS:
            normalized[field] = str(value or "")
        elif field == "year":
            normalized[field] = int(float(value or 0))
        else:
            normalized[field] = float(value or 0)
    if not normalized["pay_date"]:
        raise ValueError("Every pay statement needs a pay date.")
    return normalized


def paystub_signature(record: dict[str, object]) -> str:
    material = "|".join(
        [
            str(record.get("pay_date", "")),
            str(record.get("period_begin", "")),
            str(record.get("period_end", "")),
            f"{float(record.get('gross_pay', 0) or 0):.2f}",
            f"{float(record.get('net_pay', 0) or 0):.2f}",
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class PayPulseDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS sessions_expires_at ON sessions(expires_at);

                CREATE TABLE IF NOT EXISTS planners (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    payload TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paystubs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    signature TEXT NOT NULL,
                    pay_date TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(user_id, signature)
                );

                CREATE INDEX IF NOT EXISTS paystubs_user_date
                    ON paystubs(user_id, pay_date);
                """
            )

    def has_users(self) -> bool:
        with self.connect() as connection:
            return bool(connection.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0])

    @staticmethod
    def _public_user(row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def register_user(
        self,
        username: object,
        password: object,
        *,
        role: str | None = None,
        legacy_planner: dict[str, object] | None = None,
        legacy_paystubs: Iterable[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        clean_username = validate_username(username)
        clean_password = validate_password(password)
        if role is not None and role not in {"admin", "member"}:
            raise ValueError("Role must be admin or member.")
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            first_user = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            assigned_role = role if role is not None else ("admin" if first_user else "member")
            if not first_user and assigned_role == "admin" and role is None:
                assigned_role = "member"
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO users(username, password_hash, role, active, created_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (clean_username, hash_password(clean_password), assigned_role, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("That username is already registered.") from exc
            user_id = int(cursor.lastrowid)
            if first_user and legacy_planner is not None:
                connection.execute(
                    "INSERT INTO planners(user_id, payload, updated_at) VALUES (?, ?, ?)",
                    (user_id, json.dumps(legacy_planner, separators=(",", ":")), now),
                )
            if first_user and legacy_paystubs is not None:
                self._insert_paystub_records(connection, user_id, legacy_paystubs, now)
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._public_user(row)

    def authenticate(self, username: object, password: object) -> dict[str, object] | None:
        clean_username = str(username or "").strip()
        clean_password = str(password or "")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (clean_username,)
            ).fetchone()
        if not row or not row["active"] or not verify_password(clean_password, row["password_hash"]):
            return None
        return self._public_user(row)

    def create_session(self, user_id: int) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        csrf_token = secrets.token_urlsafe(24)
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                """
                INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (token_hash, user_id, csrf_token, now, now + SESSION_SECONDS),
            )
        return token, csrf_token

    def session_user(self, token: str | None) -> tuple[dict[str, object], str] | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = int(time.time())
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT users.*, sessions.csrf_token
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.active = 1
                """,
                (token_hash, now),
            ).fetchone()
        if not row:
            return None
        return self._public_user(row), row["csrf_token"]

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def list_users(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT users.*,
                       (SELECT COUNT(*) FROM paystubs WHERE paystubs.user_id = users.id) AS paystub_count
                FROM users ORDER BY lower(username)
                """
            ).fetchall()
        users = []
        for row in rows:
            user = self._public_user(row)
            user["paystub_count"] = row["paystub_count"]
            users.append(user)
        return users

    def update_user(self, user_id: int, *, role: object = None, active: object = None) -> dict[str, object]:
        if active is not None and not isinstance(active, bool):
            raise ValueError("Active status must be true or false.")
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise LookupError("User not found.")
            next_role = str(role) if role is not None else row["role"]
            next_active = bool(active) if active is not None else bool(row["active"])
            if next_role not in {"admin", "member"}:
                raise ValueError("Role must be admin or member.")
            removing_admin = row["role"] == "admin" and row["active"] and (
                next_role != "admin" or not next_active
            )
            if removing_admin:
                admin_count = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
                ).fetchone()[0]
                if admin_count <= 1:
                    raise ValueError("PayPulse must keep at least one active administrator.")
            now = int(time.time())
            connection.execute(
                "UPDATE users SET role = ?, active = ?, updated_at = ? WHERE id = ?",
                (next_role, int(next_active), now, user_id),
            )
            if not next_active:
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            updated = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._public_user(updated)

    def delete_user(self, user_id: int) -> None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise LookupError("User not found.")
            if row["role"] == "admin" and row["active"]:
                admin_count = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
                ).fetchone()[0]
                if admin_count <= 1:
                    raise ValueError("PayPulse must keep at least one active administrator.")
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def get_planner(self, user_id: int, default_planner: dict[str, object]) -> tuple[bool, dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM planners WHERE user_id = ?", (user_id,)
            ).fetchone()
        return (True, json.loads(row["payload"])) if row else (False, default_planner)

    def set_planner(self, user_id: int, planner: dict[str, object]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO planners(user_id, payload, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
                """,
                (user_id, json.dumps(planner, separators=(",", ":")), int(time.time())),
            )

    @staticmethod
    def _insert_paystub_records(
        connection: sqlite3.Connection,
        user_id: int,
        records: Iterable[dict[str, object]],
        now: int | None = None,
    ) -> tuple[int, int]:
        added = 0
        duplicates = 0
        timestamp = now or int(time.time())
        for raw_record in records:
            record = normalize_paystub_record(raw_record)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO paystubs(user_id, signature, pay_date, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    paystub_signature(record),
                    record["pay_date"],
                    json.dumps(record, separators=(",", ":")),
                    timestamp,
                ),
            )
            if cursor.rowcount:
                added += 1
            else:
                duplicates += 1
        return added, duplicates

    def add_statements(self, user_id: int, statements: list[ParsedStatement]) -> dict[str, object]:
        results: list[dict[str, object]] = []
        added = 0
        duplicates = 0
        with self.connect() as connection:
            for statement in statements:
                record = normalize_paystub_record(statement.record)
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO paystubs(user_id, signature, pay_date, payload, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        paystub_signature(record),
                        record["pay_date"],
                        json.dumps(record, separators=(",", ":")),
                        int(time.time()),
                    ),
                )
                status = "added" if cursor.rowcount else "duplicate"
                added += int(bool(cursor.rowcount))
                duplicates += int(not cursor.rowcount)
                results.append(statement.public_summary(status))
            total = connection.execute(
                "SELECT COUNT(*) FROM paystubs WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        return {
            "status": "added" if added else "duplicate",
            "added": added,
            "duplicates": duplicates,
            "total_records": total,
            "rows": results,
        }

    def add_paystub_records(
        self, user_id: int, records: Iterable[dict[str, object]]
    ) -> dict[str, int | str]:
        """Persist normalized CSV/API records without duplicating a user's statements."""
        record_list = list(records)
        if len(record_list) > 10_000:
            raise ValueError("A payroll import cannot contain more than 10,000 statements.")
        with self.connect() as connection:
            added, duplicates = self._insert_paystub_records(connection, user_id, record_list)
            total = connection.execute(
                "SELECT COUNT(*) FROM paystubs WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        return {
            "status": "added" if added else "duplicate",
            "added": added,
            "duplicates": duplicates,
            "total_records": total,
        }

    def list_paystubs(self, user_id: int) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM paystubs WHERE user_id = ? ORDER BY pay_date, id",
                (user_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]


def load_legacy_paystubs(path: str | Path) -> list[dict[str, object]]:
    csv_path = Path(path)
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != CSV_FIELDS:
            raise ValueError("The legacy pay-history CSV schema is not supported.")
        return [normalize_paystub_record(row) for row in reader]
