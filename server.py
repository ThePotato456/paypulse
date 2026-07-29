"""Self-hosted PayPulse server with SQLite accounts and payroll storage."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import tempfile
import uuid
from http.cookies import SimpleCookie
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from database import PayPulseDatabase, SESSION_SECONDS, load_legacy_paystubs
from ingestion import IngestionError, parse_pdf


PROJECT_DIR = Path(__file__).resolve().parent
CSV_PATH = PROJECT_DIR / "data" / "paystubs.csv"
PLANNER_PATH = PROJECT_DIR / "data" / "planner.json"
DATABASE_PATH = PROJECT_DIR / "data" / "paypulse.db"
DATABASE = PayPulseDatabase(DATABASE_PATH)
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_PLANNER_BYTES = 128 * 1024
MAX_PAYSTUB_IMPORT_BYTES = 4 * 1024 * 1024
DEFAULT_PLANNER = {
    "allocations": {
        "mode": "percent",
        "values": {"needs": 50.0, "savings": 20.0, "debt": 15.0, "flexible": 15.0},
    },
    "expenses": [],
    "goals": [],
}
EXPENSE_FREQUENCIES = {"weekly", "biweekly", "monthly", "annual", "one-time"}
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _bounded_number(value: object, maximum: float = 1_000_000_000) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Planner amounts must be valid numbers.") from exc
    if not 0 <= number <= maximum:
        raise ValueError("Planner amounts must be between 0 and 1,000,000,000.")
    return round(number, 2)


def _clean_id(value: object) -> str:
    candidate = str(value or "")
    return candidate if SAFE_ID.fullmatch(candidate) else uuid.uuid4().hex


def normalize_planner(payload: object) -> dict[str, object]:
    """Validate and sanitize the persisted financial-planning document."""
    if not isinstance(payload, dict):
        raise ValueError("Planner data must be a JSON object.")

    allocation_payload = payload.get("allocations", {})
    if not isinstance(allocation_payload, dict):
        raise ValueError("Allocations must be a JSON object.")
    mode = allocation_payload.get("mode", "percent")
    if mode not in {"percent", "amount"}:
        raise ValueError("Allocation mode must be percent or amount.")
    raw_values = allocation_payload.get("values", allocation_payload)
    if not isinstance(raw_values, dict):
        raise ValueError("Allocation values must be a JSON object.")
    allocations = {
        key: _bounded_number(raw_values.get(key, DEFAULT_PLANNER["allocations"]["values"][key]))
        for key in ("needs", "savings", "debt", "flexible")
    }

    raw_expenses = payload.get("expenses", [])
    if not isinstance(raw_expenses, list) or len(raw_expenses) > 200:
        raise ValueError("Expenses must be a list containing no more than 200 items.")
    expenses = []
    for item in raw_expenses:
        if not isinstance(item, dict):
            raise ValueError("Every expense must be a JSON object.")
        name = str(item.get("name", "")).strip()[:80]
        if not name:
            raise ValueError("Every expense needs a name.")
        frequency = str(item.get("frequency", "monthly"))
        if frequency not in EXPENSE_FREQUENCIES:
            raise ValueError("Expense frequency is not supported.")
        expenses.append(
            {
                "id": _clean_id(item.get("id")),
                "name": name,
                "category": str(item.get("category", "Other")).strip()[:40] or "Other",
                "amount": _bounded_number(item.get("amount", 0)),
                "frequency": frequency,
            }
        )

    raw_goals = payload.get("goals", [])
    if not isinstance(raw_goals, list) or len(raw_goals) > 100:
        raise ValueError("Goals must be a list containing no more than 100 items.")
    goals = []
    for item in raw_goals:
        if not isinstance(item, dict):
            raise ValueError("Every goal must be a JSON object.")
        name = str(item.get("name", "")).strip()[:80]
        if not name:
            raise ValueError("Every goal needs a name.")
        target_date = str(item.get("date", ""))
        if target_date and not ISO_DATE.fullmatch(target_date):
            raise ValueError("Goal dates must use YYYY-MM-DD format.")
        goals.append(
            {
                "id": _clean_id(item.get("id")),
                "name": name,
                "target": _bounded_number(item.get("target", 0)),
                "saved": _bounded_number(item.get("saved", 0)),
                "date": target_date,
            }
        )

    return {
        "allocations": {"mode": mode, "values": allocations},
        "expenses": expenses,
        "goals": goals,
    }


def load_planner(path: Path = PLANNER_PATH) -> dict[str, object]:
    if not path.exists():
        return normalize_planner(DEFAULT_PLANNER)
    try:
        return normalize_planner(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("The saved planner data is invalid or unreadable.") from exc


def save_planner(payload: object, path: Path = PLANNER_PATH) -> dict[str, object]:
    planner = normalize_planner(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(planner, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return planner


class PayPulseHandler(SimpleHTTPRequestHandler):
    """Serve the dashboard and handle same-origin PDF ingestion."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        request_path = urlparse(self.path).path
        if (
            request_path.startswith("/api/")
            or request_path == "/"
            or Path(request_path).suffix.lower() in {".html", ".js", ".css", ".csv"}
        ):
            self.send_header("Cache-Control", "no-store")
            self.send_header("CDN-Cache-Control", "no-store")
            self.send_header("Cloudflare-CDN-Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        request_path = urlparse(self.path).path
        if request_path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "ingestion": True,
                    "planner_persistence": True,
                    "authentication": True,
                    "database": True,
                    "payroll_import": True,
                },
            )
            return
        if request_path == "/api/auth/session":
            session = self._current_session()
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "authenticated": bool(session),
                    "user": session[0] if session else None,
                    "csrf_token": session[1] if session else None,
                },
            )
            return
        if request_path == "/api/paystubs":
            session = self._require_user()
            if not session:
                return
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "paystubs": DATABASE.list_paystubs(int(session[0]["id"]))},
            )
            return
        if request_path == "/api/planner":
            session = self._require_user()
            if not session:
                return
            persisted, planner = DATABASE.get_planner(
                int(session[0]["id"]), normalize_planner(DEFAULT_PLANNER)
            )
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "persisted": persisted, "planner": planner},
            )
            return
        if request_path == "/api/users":
            session = self._require_user(admin=True)
            if not session:
                return
            self._send_json(HTTPStatus.OK, {"status": "ok", "users": DATABASE.list_users()})
            return
        if request_path.startswith("/data/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Not found."})
            return
        super().do_GET()

    def do_PUT(self) -> None:
        if urlparse(self.path).path != "/api/planner":
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Not found."})
            return
        session = self._require_user(csrf=True)
        if not session:
            return
        try:
            payload = self._read_json_body()
            planner = normalize_planner(payload)
            DATABASE.set_planner(int(session[0]["id"]), planner)
            self._send_json(HTTPStatus.OK, {"status": "ok", "planner": planner})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})
        except OSError:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"status": "error", "message": "Planner data could not be saved."},
            )

    def do_POST(self) -> None:
        request_path = urlparse(self.path).path
        if request_path == "/api/auth/register":
            self._register()
            return
        if request_path == "/api/auth/login":
            self._login()
            return
        if request_path == "/api/auth/logout":
            session = self._require_user(csrf=True)
            if not session:
                return
            DATABASE.delete_session(self._session_token())
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok"},
                cookie="paypulse_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
            )
            return
        if request_path == "/api/users":
            session = self._require_user(admin=True, csrf=True)
            if not session:
                return
            try:
                payload = self._read_json_body()
                if not isinstance(payload, dict):
                    raise ValueError("User data must be a JSON object.")
                user = DATABASE.register_user(
                    payload.get("username"), payload.get("password"), role=str(payload.get("role", "member"))
                )
                self._send_json(HTTPStatus.CREATED, {"status": "ok", "user": user})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})
            return
        if request_path == "/api/paystubs/import":
            session = self._require_user(csrf=True)
            if not session:
                return
            try:
                payload = self._read_json_body(MAX_PAYSTUB_IMPORT_BYTES)
                if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                    raise ValueError("Payroll import data must contain a records list.")
                result = DATABASE.add_paystub_records(
                    int(session[0]["id"]), payload["records"]
                )
                self._send_json(HTTPStatus.OK, result)
            except ValueError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)}
                )
            return
        if request_path != "/api/ingest":
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Not found."})
            return

        session = self._require_user(csrf=True)
        if not session:
            return

        try:
            filename, pdf_bytes = self._read_pdf_upload()
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporary:
                temporary.write(pdf_bytes)
                temporary_path = Path(temporary.name)
            try:
                statements = parse_pdf(temporary_path)
                result = DATABASE.add_statements(int(session[0]["id"]), statements)
            finally:
                temporary_path.unlink(missing_ok=True)

            result["filename"] = Path(filename).name
            self._send_json(HTTPStatus.OK, result)
        except IngestionError as exc:
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"status": "error", "message": str(exc)},
            )
        except ValueError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "message": str(exc)},
            )
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "status": "error",
                    "message": "The paystub could not be processed. No database changes were made.",
                },
            )

    def do_PATCH(self) -> None:
        match = re.fullmatch(r"/api/users/(\d+)", urlparse(self.path).path)
        if not match:
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Not found."})
            return
        session = self._require_user(admin=True, csrf=True)
        if not session:
            return
        try:
            payload = self._read_json_body()
            if not isinstance(payload, dict):
                raise ValueError("User data must be a JSON object.")
            target_id = int(match.group(1))
            if target_id == int(session[0]["id"]) and payload.get("active") is False:
                raise ValueError("You cannot deactivate your current account.")
            user = DATABASE.update_user(
                target_id,
                role=payload.get("role"),
                active=payload.get("active"),
            )
            self._send_json(HTTPStatus.OK, {"status": "ok", "user": user})
        except LookupError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})

    def do_DELETE(self) -> None:
        match = re.fullmatch(r"/api/users/(\d+)", urlparse(self.path).path)
        if not match:
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Not found."})
            return
        session = self._require_user(admin=True, csrf=True)
        if not session:
            return
        target_id = int(match.group(1))
        if target_id == int(session[0]["id"]):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "message": "You cannot delete your current account."},
            )
            return
        try:
            DATABASE.delete_user(target_id)
            self._send_json(HTTPStatus.OK, {"status": "ok"})
        except LookupError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})

    def _register(self) -> None:
        try:
            payload = self._read_json_body()
            if not isinstance(payload, dict):
                raise ValueError("Registration data must be a JSON object.")
            first_account = not DATABASE.has_users()
            legacy_planner = (
                load_planner(PLANNER_PATH) if first_account and PLANNER_PATH.exists() else None
            )
            legacy_paystubs = load_legacy_paystubs(CSV_PATH) if first_account else None
            user = DATABASE.register_user(
                payload.get("username"),
                payload.get("password"),
                legacy_planner=legacy_planner,
                legacy_paystubs=legacy_paystubs,
            )
            token, csrf_token = DATABASE.create_session(int(user["id"]))
            self._send_json(
                HTTPStatus.CREATED,
                {"status": "ok", "user": user, "csrf_token": csrf_token},
                cookie=self._session_cookie(token),
            )
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})

    def _login(self) -> None:
        try:
            payload = self._read_json_body()
            if not isinstance(payload, dict):
                raise ValueError("Login data must be a JSON object.")
            user = DATABASE.authenticate(payload.get("username"), payload.get("password"))
            if not user:
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"status": "error", "message": "Username or password is incorrect."},
                )
                return
            token, csrf_token = DATABASE.create_session(int(user["id"]))
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "user": user, "csrf_token": csrf_token},
                cookie=self._session_cookie(token),
            )
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})

    def _session_token(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get("paypulse_session")
        return morsel.value if morsel else None

    def _current_session(self) -> tuple[dict[str, object], str] | None:
        return DATABASE.session_user(self._session_token())

    def _require_user(
        self, *, admin: bool = False, csrf: bool = False
    ) -> tuple[dict[str, object], str] | None:
        session = self._current_session()
        if not session:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"status": "error", "message": "Sign in to continue."},
            )
            return None
        user, csrf_token = session
        if admin and user["role"] != "admin":
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"status": "error", "message": "Administrator access is required."},
            )
            return None
        if csrf and not hmac.compare_digest(
            self.headers.get("X-CSRF-Token", ""), csrf_token
        ):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"status": "error", "message": "Security token is missing or invalid."},
            )
            return None
        return session

    @staticmethod
    def _session_cookie(token: str) -> str:
        return (
            f"paypulse_session={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_SECONDS}"
        )

    def _read_pdf_upload(self) -> tuple[str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type.lower():
            raise ValueError("Upload must use multipart form data.")

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid upload size.") from exc
        if content_length <= 0:
            raise ValueError("The upload is empty.")
        if content_length > MAX_UPLOAD_BYTES:
            raise ValueError("The PDF exceeds the 15 MB upload limit.")

        body = self.rfile.read(content_length)
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: "
            + content_type.encode("utf-8")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + body
        )
        for part in message.iter_parts():
            if part.get_param("name", header="content-disposition") != "file":
                continue
            filename = part.get_filename() or "paystub.pdf"
            payload = part.get_payload(decode=True) or b""
            if Path(filename).suffix.lower() != ".pdf":
                raise ValueError("Only PDF files are supported.")
            if not payload.startswith(b"%PDF"):
                raise ValueError("The uploaded file is not a valid PDF.")
            return filename, payload
        raise ValueError("No PDF file was included in the upload.")

    def _read_json_body(self, maximum_bytes: int = MAX_PLANNER_BYTES) -> object:
        if "application/json" not in self.headers.get("Content-Type", "").lower():
            raise ValueError("JSON requests must use application/json.")
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid JSON request size.") from exc
        if content_length <= 0:
            raise ValueError("JSON request data is empty.")
        if content_length > maximum_bytes:
            limit = (
                f"{maximum_bytes // (1024 * 1024)} MB"
                if maximum_bytes >= 1024 * 1024
                else f"{maximum_bytes // 1024} KB"
            )
            raise ValueError(f"JSON request data exceeds the {limit} limit.")
        try:
            return json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request data must be valid UTF-8 JSON.") from exc

    def _send_json(
        self, status: HTTPStatus, payload: dict[str, object], *, cookie: str | None = None
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the self-hosted PayPulse dashboard.")
    parser.add_argument(
        "--host",
        default=os.environ.get("PAY_DASHBOARD_HOST", "127.0.0.1"),
        help="Interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PAY_DASHBOARD_PORT", "8000")),
        help="Port to bind (default: 8000)",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=PROJECT_DIR / "data" / "paystubs.csv",
        help="Legacy pay-history CSV imported into the first account",
    )
    parser.add_argument(
        "--planner",
        type=Path,
        default=PROJECT_DIR / "data" / "planner.json",
        help="Legacy planner JSON imported into the first account",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_DIR / "data" / "paypulse.db",
        help="SQLite database path",
    )
    arguments = parser.parse_args()

    global CSV_PATH, PLANNER_PATH, DATABASE_PATH, DATABASE
    CSV_PATH = arguments.csv.resolve()
    PLANNER_PATH = arguments.planner.resolve()
    DATABASE_PATH = arguments.database.resolve()
    DATABASE = PayPulseDatabase(DATABASE_PATH)
    DATABASE.initialize()
    host = arguments.host
    port = arguments.port
    server = ThreadingHTTPServer((host, port), PayPulseHandler)
    print(f"PayPulse is running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
