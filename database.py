"""SQLite persistence, authentication, and encrypted financial storage for PayPulse."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path
from typing import Iterable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ingestion import CSV_FIELDS, ParsedStatement


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
PASSWORD_ITERATIONS = 310_000
SESSION_SECONDS = 7 * 24 * 60 * 60
CRYPTO_VERSION = 1
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
TEXT_PAYSTUB_FIELDS = {"pay_date", "period_begin", "period_end", "pay_type", "payment_type"}
RECOVERY_PUBLIC_KEY = "recovery_public_key_v1"
RECOVERY_PRIVATE_KEY = "recovery_private_key_v1"
PLAINTEXT_CLEANUP_REQUIRED = "plaintext_cleanup_required_v1"


class VaultError(RuntimeError):
    """Raised when encrypted financial data cannot be safely unlocked."""


class ClosingConnection(sqlite3.Connection):
    """Commit or roll back a context block, then release the SQLite file handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def _encrypt_bytes(key: bytes, plaintext: bytes, aad: bytes) -> str:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return json.dumps(
        {"v": CRYPTO_VERSION, "alg": "A256GCM", "nonce": _b64(nonce), "ct": _b64(ciphertext)},
        separators=(",", ":"),
    )


def _decrypt_bytes(key: bytes, envelope: str, aad: bytes) -> bytes:
    try:
        payload = json.loads(envelope)
        if payload.get("v") != CRYPTO_VERSION or payload.get("alg") != "A256GCM":
            raise VaultError("Encrypted data uses an unsupported format.")
        return AESGCM(key).decrypt(_unb64(payload["nonce"]), _unb64(payload["ct"]), aad)
    except VaultError:
        raise
    except (InvalidTag, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VaultError("Encrypted financial data failed authentication.") from exc


def _derive_password_key(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(
        password.encode("utf-8")
    )


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
    return "pbkdf2_sha256${}${}${}".format(iterations, _b64(salt), _b64(digest))


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _unb64(salt), int(iterations)
        )
        return hmac.compare_digest(_b64(digest), expected)
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
    income_frequency = str(record.get("income_frequency") or "").strip()
    if income_frequency in {"one-time", "weekly", "biweekly", "semimonthly", "monthly", "annual"}:
        normalized["income_frequency"] = income_frequency
    income_type = str(record.get("income_type") or "").strip()
    if income_type:
        normalized["income_type"] = income_type[:32]
    return normalized


def paystub_signature(record: dict[str, object]) -> str:
    parts = [
        str(record.get("pay_date", "")),
        str(record.get("period_begin", "")),
        str(record.get("period_end", "")),
        f"{float(record.get('gross_pay', 0) or 0):.2f}",
        f"{float(record.get('net_pay', 0) or 0):.2f}",
    ]
    if str(record.get("pay_type", "")).startswith("Manual:"):
        parts.append(str(record.get("pay_type", "")))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class PayPulseDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._session_keys: dict[str, tuple[bytes, bytes | None]] = {}
        self._key_lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
        return connection

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
        name = definition.split()[0]
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

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
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS sessions_expires_at ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS paystubs_user_date ON paystubs(user_id, pay_date);
                """
            )
            for definition in (
                "is_owner INTEGER NOT NULL DEFAULT 0",
                "must_change_password INTEGER NOT NULL DEFAULT 0",
                "crypto_version INTEGER NOT NULL DEFAULT 0",
                "kdf_salt TEXT",
                "wrapped_vault_key TEXT",
                "recovery_wrapped_vault_key TEXT",
            ):
                self._ensure_column(connection, "users", definition)
            if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] and not connection.execute(
                "SELECT EXISTS(SELECT 1 FROM users WHERE is_owner = 1)"
            ).fetchone()[0]:
                connection.execute(
                    "UPDATE users SET is_owner = 1, role = 'admin' WHERE id = (SELECT MIN(id) FROM users)"
                )
            # Vault keys live only in this process, so cookies from an earlier process are unusable.
            connection.execute("DELETE FROM sessions")
        with self._key_lock:
            self._session_keys.clear()
        self._purge_plaintext_remnants_if_required()

    def _purge_plaintext_remnants_if_required(self) -> None:
        with self.connect() as connection:
            required = self._setting(connection, PLAINTEXT_CLEANUP_REQUIRED)
        if not required:
            return
        try:
            with self.connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
                connection.execute(
                    "DELETE FROM settings WHERE key = ?", (PLAINTEXT_CLEANUP_REQUIRED,)
                )
            with self.connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            raise VaultError(
                "Encrypted migration finished, but SQLite plaintext cleanup is still pending."
            ) from exc

    def has_users(self) -> bool:
        with self.connect() as connection:
            return bool(connection.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0])

    @staticmethod
    def _public_user(row: sqlite3.Row) -> dict[str, object]:
        keys = set(row.keys())
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "active": bool(row["active"]),
            "is_owner": bool(row["is_owner"]) if "is_owner" in keys else False,
            "must_change_password": bool(row["must_change_password"])
            if "must_change_password" in keys
            else False,
            "encryption_migrated": bool(row["crypto_version"])
            if "crypto_version" in keys
            else False,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _vault_aad(user_id: int) -> bytes:
        return f"paypulse:v1:vault-key:{user_id}".encode()

    @staticmethod
    def _planner_aad(user_id: int) -> bytes:
        return f"paypulse:v1:planner:{user_id}".encode()

    @staticmethod
    def _paystub_aad(user_id: int, record_id: int) -> bytes:
        return f"paypulse:v1:paystub:{user_id}:{record_id}".encode()

    @staticmethod
    def _recovery_aad(owner_id: int) -> bytes:
        return f"paypulse:v1:recovery-private:{owner_id}".encode()

    @staticmethod
    def _blind_signature(vault_key: bytes, record: dict[str, object]) -> str:
        index_key = hmac.new(vault_key, b"paypulse:v1:paystub-index", hashlib.sha256).digest()
        return hmac.new(index_key, paystub_signature(record).encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _encrypt_json(vault_key: bytes, value: dict[str, object], aad: bytes) -> str:
        return _encrypt_bytes(
            vault_key, json.dumps(value, separators=(",", ":")).encode("utf-8"), aad
        )

    @staticmethod
    def _decrypt_json(vault_key: bytes, envelope: str, aad: bytes) -> dict[str, object]:
        try:
            value = json.loads(_decrypt_bytes(vault_key, envelope, aad).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VaultError("Encrypted financial data is not valid JSON.") from exc
        if not isinstance(value, dict):
            raise VaultError("Encrypted financial data has an invalid shape.")
        return value

    @staticmethod
    def _recovery_wrap(public_pem: str, vault_key: bytes) -> str:
        public_key = serialization.load_pem_public_key(public_pem.encode("ascii"))
        ciphertext = public_key.encrypt(
            vault_key,
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        return _b64(ciphertext)

    @staticmethod
    def _recovery_unwrap(private_key: bytes, wrapped: str) -> bytes:
        key = serialization.load_pem_private_key(private_key, password=None)
        try:
            return key.decrypt(
                _unb64(wrapped),
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
        except ValueError as exc:
            raise VaultError("The recovery key could not unlock this account.") from exc

    def _setting(self, connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _create_recovery_keys(
        self, connection: sqlite3.Connection, owner_id: int, password_key: bytes
    ) -> tuple[str, bytes]:
        private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        private_pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_pem = private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")
        wrapped_private = _encrypt_bytes(
            password_key, private_pem, self._recovery_aad(owner_id)
        )
        connection.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (RECOVERY_PUBLIC_KEY, public_pem))
        connection.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (RECOVERY_PRIVATE_KEY, wrapped_private))
        return public_pem, private_pem

    def _migrate_user(
        self, connection: sqlite3.Connection, row: sqlite3.Row, password: str
    ) -> tuple[bytes, bytes | None]:
        user_id = int(row["id"])
        is_owner = bool(row["is_owner"])
        salt = secrets.token_bytes(16)
        password_key = _derive_password_key(password, salt)
        public_pem = self._setting(connection, RECOVERY_PUBLIC_KEY)
        recovery_private: bytes | None = None
        if not public_pem:
            if not is_owner:
                raise VaultError("The owner must sign in once before this account can be encrypted.")
            public_pem, recovery_private = self._create_recovery_keys(
                connection, user_id, password_key
            )
        elif is_owner:
            wrapped_private = self._setting(connection, RECOVERY_PRIVATE_KEY)
            if not wrapped_private:
                raise VaultError("The owner recovery key is missing.")
            recovery_private = _decrypt_bytes(
                password_key, wrapped_private, self._recovery_aad(user_id)
            )

        vault_key = secrets.token_bytes(32)
        wrapped_vault = _encrypt_bytes(password_key, vault_key, self._vault_aad(user_id))
        recovery_wrapped = self._recovery_wrap(public_pem, vault_key)

        paystubs = connection.execute(
            "SELECT id, payload FROM paystubs WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()
        for stored in paystubs:
            record = normalize_paystub_record(json.loads(stored["payload"]))
            record_id = int(stored["id"])
            encrypted = self._encrypt_json(
                vault_key, record, self._paystub_aad(user_id, record_id)
            )
            # Verify before replacing the only plaintext copy.
            self._decrypt_json(vault_key, encrypted, self._paystub_aad(user_id, record_id))
            connection.execute(
                "UPDATE paystubs SET signature = ?, pay_date = '', payload = ? WHERE id = ?",
                (self._blind_signature(vault_key, record), encrypted, record_id),
            )
        planner = connection.execute(
            "SELECT payload FROM planners WHERE user_id = ?", (user_id,)
        ).fetchone()
        if planner:
            planner_value = json.loads(planner["payload"])
            encrypted = self._encrypt_json(vault_key, planner_value, self._planner_aad(user_id))
            self._decrypt_json(vault_key, encrypted, self._planner_aad(user_id))
            connection.execute(
                "UPDATE planners SET payload = ? WHERE user_id = ?", (encrypted, user_id)
            )
        connection.execute(
            """
            UPDATE users
            SET crypto_version = ?, kdf_salt = ?, wrapped_vault_key = ?,
                recovery_wrapped_vault_key = ?, updated_at = ?
            WHERE id = ?
            """,
            (CRYPTO_VERSION, _b64(salt), wrapped_vault, recovery_wrapped, int(time.time()), user_id),
        )
        connection.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, '1')",
            (PLAINTEXT_CLEANUP_REQUIRED,),
        )
        return vault_key, recovery_private

    def _unlock_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row, password: str
    ) -> tuple[bytes, bytes | None]:
        if not row["crypto_version"]:
            return self._migrate_user(connection, row, password)
        password_key = _derive_password_key(password, _unb64(row["kdf_salt"]))
        vault_key = _decrypt_bytes(
            password_key, row["wrapped_vault_key"], self._vault_aad(int(row["id"]))
        )
        recovery_private = None
        if row["is_owner"]:
            wrapped_private = self._setting(connection, RECOVERY_PRIVATE_KEY)
            if not wrapped_private:
                raise VaultError("The owner recovery key is missing.")
            recovery_private = _decrypt_bytes(
                password_key, wrapped_private, self._recovery_aad(int(row["id"]))
            )
        return vault_key, recovery_private

    def register_user(
        self,
        username: object,
        password: object,
        *,
        role: str | None = None,
        require_password_change: bool = False,
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
            if not first_user and not self._setting(connection, RECOVERY_PUBLIC_KEY):
                raise VaultError("The owner must sign in once before another account can be created.")
            assigned_role = "admin" if first_user else (role if role is not None else "member")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO users(
                        username, password_hash, role, active, is_owner, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        clean_username,
                        hash_password(clean_password),
                        assigned_role,
                        int(first_user),
                        now,
                        now,
                    ),
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
                self._insert_plaintext_records(connection, user_id, legacy_paystubs, now)
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            self._migrate_user(connection, row, clean_password)
            if require_password_change:
                connection.execute(
                    "UPDATE users SET must_change_password = 1 WHERE id = ?", (user_id,)
                )
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        self._purge_plaintext_remnants_if_required()
        return self._public_user(row)

    def unlock_user(
        self, username: object, password: object
    ) -> tuple[dict[str, object], bytes, bytes | None] | None:
        clean_username = str(username or "").strip()
        clean_password = str(password or "")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (clean_username,)
            ).fetchone()
            if not row or not row["active"] or not verify_password(
                clean_password, row["password_hash"]
            ):
                return None
            migrated = not bool(row["crypto_version"])
            vault_key, recovery_private = self._unlock_row(connection, row, clean_password)
            row = connection.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
        if migrated:
            self._purge_plaintext_remnants_if_required()
        return self._public_user(row), vault_key, recovery_private

    def authenticate(self, username: object, password: object) -> dict[str, object] | None:
        unlocked = self.unlock_user(username, password)
        return unlocked[0] if unlocked else None

    def create_session(
        self, user_id: int, vault_key: bytes | None = None, recovery_private: bytes | None = None
    ) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        csrf_token = secrets.token_urlsafe(24)
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (token_hash, user_id, csrf_token, now, now + SESSION_SECONDS),
            )
        if vault_key is not None:
            with self._key_lock:
                self._session_keys[token_hash] = (vault_key, recovery_private)
        return token, csrf_token

    def session_user(
        self, token: str | None
    ) -> tuple[dict[str, object], str, bytes | None, bytes | None] | None:
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
        with self._key_lock:
            keys = self._session_keys.get(token_hash, (None, None))
        return self._public_user(row), row["csrf_token"], keys[0], keys[1]

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        with self._key_lock:
            self._session_keys.pop(token_hash, None)

    def _clear_user_sessions(self, connection: sqlite3.Connection, user_id: int) -> None:
        hashes_to_remove = [
            row["token_hash"]
            for row in connection.execute("SELECT token_hash FROM sessions WHERE user_id = ?", (user_id,))
        ]
        connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        with self._key_lock:
            for token_hash in hashes_to_remove:
                self._session_keys.pop(token_hash, None)

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
            if row["is_owner"] and (next_role != "admin" or not next_active):
                raise ValueError("The recovery owner cannot be demoted or deactivated.")
            connection.execute(
                "UPDATE users SET role = ?, active = ?, updated_at = ? WHERE id = ?",
                (next_role, int(next_active), int(time.time()), user_id),
            )
            if not next_active:
                self._clear_user_sessions(connection, user_id)
            updated = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._public_user(updated)

    def delete_user(self, user_id: int) -> None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise LookupError("User not found.")
            if row["is_owner"]:
                raise ValueError("The recovery owner cannot be deleted.")
            self._clear_user_sessions(connection, user_id)
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def change_password(
        self,
        user_id: int,
        current_password: object,
        new_password: object,
        vault_key: bytes,
        recovery_private: bytes | None = None,
    ) -> None:
        current = str(current_password or "")
        new = validate_password(new_password)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row or not verify_password(current, row["password_hash"]):
                raise ValueError("Current password is incorrect.")
            salt = secrets.token_bytes(16)
            password_key = _derive_password_key(new, salt)
            wrapped = _encrypt_bytes(password_key, vault_key, self._vault_aad(user_id))
            if row["is_owner"]:
                if not recovery_private:
                    raise VaultError("Sign in again before changing the owner password.")
                connection.execute(
                    "UPDATE settings SET value = ? WHERE key = ?",
                    (
                        _encrypt_bytes(
                            password_key, recovery_private, self._recovery_aad(user_id)
                        ),
                        RECOVERY_PRIVATE_KEY,
                    ),
                )
            connection.execute(
                """
                UPDATE users SET password_hash = ?, kdf_salt = ?, wrapped_vault_key = ?,
                    must_change_password = 0, updated_at = ? WHERE id = ?
                """,
                (hash_password(new), _b64(salt), wrapped, int(time.time()), user_id),
            )
            self._clear_user_sessions(connection, user_id)

    def reset_password(
        self,
        owner_id: int,
        target_id: int,
        temporary_password: object,
        recovery_private: bytes,
    ) -> dict[str, object]:
        temporary = validate_password(temporary_password)
        if owner_id == target_id:
            raise ValueError("The recovery owner must change their password using the current password.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute("SELECT * FROM users WHERE id = ?", (owner_id,)).fetchone()
            target = connection.execute("SELECT * FROM users WHERE id = ?", (target_id,)).fetchone()
            if not owner or not owner["is_owner"]:
                raise PermissionError("Only the recovery owner can reset encrypted accounts.")
            if not target:
                raise LookupError("User not found.")
            vault_key = self._recovery_unwrap(
                recovery_private, target["recovery_wrapped_vault_key"]
            )
            salt = secrets.token_bytes(16)
            password_key = _derive_password_key(temporary, salt)
            wrapped = _encrypt_bytes(password_key, vault_key, self._vault_aad(target_id))
            connection.execute(
                """
                UPDATE users SET password_hash = ?, kdf_salt = ?, wrapped_vault_key = ?,
                    must_change_password = 1, updated_at = ? WHERE id = ?
                """,
                (hash_password(temporary), _b64(salt), wrapped, int(time.time()), target_id),
            )
            self._clear_user_sessions(connection, target_id)
            updated = connection.execute("SELECT * FROM users WHERE id = ?", (target_id,)).fetchone()
        return self._public_user(updated)

    def get_planner(
        self, user_id: int, default_planner: dict[str, object], vault_key: bytes
    ) -> tuple[bool, dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute("SELECT payload FROM planners WHERE user_id = ?", (user_id,)).fetchone()
        return (
            (True, self._decrypt_json(vault_key, row["payload"], self._planner_aad(user_id)))
            if row
            else (False, default_planner)
        )

    def set_planner(self, user_id: int, planner: dict[str, object], vault_key: bytes) -> None:
        encrypted = self._encrypt_json(vault_key, planner, self._planner_aad(user_id))
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO planners(user_id, payload, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
                """,
                (user_id, encrypted, int(time.time())),
            )

    @staticmethod
    def _insert_plaintext_records(
        connection: sqlite3.Connection,
        user_id: int,
        records: Iterable[dict[str, object]],
        now: int | None = None,
    ) -> tuple[int, int]:
        added = duplicates = 0
        timestamp = now or int(time.time())
        for raw_record in records:
            record = normalize_paystub_record(raw_record)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO paystubs(user_id, signature, pay_date, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    user_id,
                    paystub_signature(record),
                    record["pay_date"],
                    json.dumps(record, separators=(",", ":")),
                    timestamp,
                ),
            )
            added += int(bool(cursor.rowcount))
            duplicates += int(not cursor.rowcount)
        return added, duplicates

    def _insert_paystub_records(
        self,
        connection: sqlite3.Connection,
        user_id: int,
        records: Iterable[dict[str, object]],
        vault_key: bytes,
        now: int | None = None,
    ) -> tuple[int, int]:
        added = duplicates = 0
        timestamp = now or int(time.time())
        for raw_record in records:
            record = normalize_paystub_record(raw_record)
            signature = self._blind_signature(vault_key, record)
            try:
                cursor = connection.execute(
                    "INSERT INTO paystubs(user_id, signature, pay_date, payload, created_at) VALUES (?, ?, '', '', ?)",
                    (user_id, signature, timestamp),
                )
            except sqlite3.IntegrityError:
                duplicates += 1
                continue
            record_id = int(cursor.lastrowid)
            encrypted = self._encrypt_json(
                vault_key, record, self._paystub_aad(user_id, record_id)
            )
            connection.execute("UPDATE paystubs SET payload = ? WHERE id = ?", (encrypted, record_id))
            added += 1
        return added, duplicates

    def add_statements(
        self, user_id: int, statements: list[ParsedStatement], vault_key: bytes
    ) -> dict[str, object]:
        results = []
        added = duplicates = 0
        with self.connect() as connection:
            for statement in statements:
                row_added, row_duplicate = self._insert_paystub_records(
                    connection, user_id, [statement.record], vault_key
                )
                status = "added" if row_added else "duplicate"
                added += row_added
                duplicates += row_duplicate
                results.append(statement.public_summary(status))
            total = connection.execute("SELECT COUNT(*) FROM paystubs WHERE user_id = ?", (user_id,)).fetchone()[0]
        return {"status": "added" if added else "duplicate", "added": added, "duplicates": duplicates, "total_records": total, "rows": results}

    def add_paystub_records(
        self, user_id: int, records: Iterable[dict[str, object]], vault_key: bytes
    ) -> dict[str, int | str]:
        record_list = list(records)
        if len(record_list) > 10_000:
            raise ValueError("A payroll import cannot contain more than 10,000 statements.")
        with self.connect() as connection:
            added, duplicates = self._insert_paystub_records(
                connection, user_id, record_list, vault_key
            )
            total = connection.execute("SELECT COUNT(*) FROM paystubs WHERE user_id = ?", (user_id,)).fetchone()[0]
        return {"status": "added" if added else "duplicate", "added": added, "duplicates": duplicates, "total_records": total}

    def list_paystubs(self, user_id: int, vault_key: bytes) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT id, payload FROM paystubs WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
        records = []
        for row in rows:
            record = self._decrypt_json(
                vault_key, row["payload"], self._paystub_aad(user_id, int(row["id"]))
            )
            record["_record_id"] = int(row["id"])
            records.append(record)
        records.sort(key=lambda item: (str(item.get("pay_date", "")), int(item["_record_id"])))
        return records

    def update_paystub_record(
        self, user_id: int, record_id: int, raw_record: dict[str, object], vault_key: bytes
    ) -> dict[str, object]:
        record = normalize_paystub_record(raw_record)
        for field in ("pay_date", "period_begin", "period_end"):
            value = str(record.get(field) or "")
            if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(f"{field.replace('_', ' ').title()} must use YYYY-MM-DD format.")
            if value:
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError(f"{field.replace('_', ' ').title()} must be a valid calendar date.") from exc
        if bool(record["period_begin"]) != bool(record["period_end"]):
            raise ValueError("Enter both pay-period dates or leave both blank.")
        if record["period_begin"] and record["period_begin"] > record["period_end"]:
            raise ValueError("Pay-period start cannot be after its end.")
        for field in CSV_FIELDS:
            if field not in TEXT_PAYSTUB_FIELDS and field != "year" and not math.isfinite(float(record[field])):
                raise ValueError(f"{field.replace('_', ' ').title()} must be a valid number.")
        for field in ("gross_pay", "total_taxes", "total_deductions", "net_pay", "hours_units", "regular_rate", "overtime_hours"):
            if float(record[field]) < 0:
                raise ValueError(f"{field.replace('_', ' ').title()} must be zero or greater.")
        expected_net = round(float(record["gross_pay"]) - float(record["total_taxes"]) - float(record["total_deductions"]), 2)
        if abs(expected_net - float(record["net_pay"])) > 0.02:
            raise ValueError("Gross minus taxes and deductions must equal net pay.")
        record["calculated_net"] = expected_net
        record["year"] = int(str(record["pay_date"])[:4])
        encrypted = self._encrypt_json(vault_key, record, self._paystub_aad(user_id, record_id))
        signature = self._blind_signature(vault_key, record)
        with self.connect() as connection:
            try:
                cursor = connection.execute(
                    "UPDATE paystubs SET signature = ?, pay_date = '', payload = ? WHERE id = ? AND user_id = ?",
                    (signature, encrypted, record_id, user_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("That change would duplicate another pay statement.") from exc
            if not cursor.rowcount:
                raise LookupError("Pay statement not found.")
        record["_record_id"] = record_id
        return record

    def delete_paystub_record(self, user_id: int, record_id: int) -> None:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM paystubs WHERE id = ? AND user_id = ?", (record_id, user_id))
            if not cursor.rowcount:
                raise LookupError("Pay statement not found.")


def load_legacy_paystubs(path: str | Path) -> list[dict[str, object]]:
    csv_path = Path(path)
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != CSV_FIELDS:
            raise ValueError("The legacy pay-history CSV schema is not supported.")
        return [normalize_paystub_record(row) for row in reader]
