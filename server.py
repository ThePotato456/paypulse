"""Self-hosted PayPulse server with static files and local paystub ingestion."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ingestion import IngestionError, append_statements, parse_pdf


PROJECT_DIR = Path(__file__).resolve().parent
CSV_PATH = PROJECT_DIR / "data" / "paystubs.csv"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
CSV_WRITE_LOCK = threading.Lock()


class PayPulseHandler(SimpleHTTPRequestHandler):
    """Serve the dashboard and handle same-origin PDF ingestion."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.path.startswith("/api/") or self.path.endswith("paystubs.csv"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "ingestion": True,
                    "csv_exists": CSV_PATH.exists(),
                },
            )
            return
        super().do_GET()

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/ingest":
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Not found."})
            return

        try:
            filename, pdf_bytes = self._read_pdf_upload()
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporary:
                temporary.write(pdf_bytes)
                temporary_path = Path(temporary.name)
            try:
                statements = parse_pdf(temporary_path)
                with CSV_WRITE_LOCK:
                    result = append_statements(CSV_PATH, statements)
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
                    "message": "The paystub could not be processed. No CSV changes were made.",
                },
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

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
        help="Pay-history CSV path",
    )
    arguments = parser.parse_args()

    global CSV_PATH
    CSV_PATH = arguments.csv.resolve()
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
