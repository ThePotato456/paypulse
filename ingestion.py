"""Extract Papa John's pay statements from PDF files and append sanitized CSV rows."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

import pdfplumber


CSV_FIELDS = [
    "pay_date",
    "period_begin",
    "period_end",
    "year",
    "pay_type",
    "payment_type",
    "gross_pay",
    "total_taxes",
    "total_deductions",
    "calculated_net",
    "net_pay",
    "hours_units",
    "regular_rate",
    "regular_hours",
    "regular_pay",
    "overtime_rate",
    "overtime_hours",
    "overtime_pay",
    "bonus_pay",
    "reported_tips",
    "social_security_tax",
    "medicare_tax",
    "federal_withholding",
    "mississippi_withholding",
    "roth_401k",
    "dental_insurance",
    "health_insurance",
]

MONEY_FIELDS = {
    "gross_pay",
    "total_taxes",
    "total_deductions",
    "calculated_net",
    "net_pay",
    "regular_pay",
    "overtime_pay",
    "bonus_pay",
    "reported_tips",
    "social_security_tax",
    "medicare_tax",
    "federal_withholding",
    "mississippi_withholding",
    "roth_401k",
    "dental_insurance",
    "health_insurance",
}

DECIMAL_FIELDS = set(CSV_FIELDS) - {
    "pay_date",
    "period_begin",
    "period_end",
    "year",
    "pay_type",
    "payment_type",
}

CENT = Decimal("0.01")
TOLERANCE = Decimal("0.02")
NUMBER_PATTERN = r"-?\$?[\d,]+(?:\.\d+)?"


class IngestionError(ValueError):
    """Raised when a PDF cannot be safely converted into a reconciled CSV row."""


@dataclass
class ParsedStatement:
    record: dict[str, object]
    checks: dict[str, object]
    source_page: int

    def public_summary(self, status: str) -> dict[str, object]:
        return {
            "status": status,
            "pay_date": self.record["pay_date"],
            "period_begin": self.record["period_begin"],
            "period_end": self.record["period_end"],
            "gross_pay": float(self.record["gross_pay"]),
            "net_pay": float(self.record["net_pay"]),
            "total_taxes": float(self.record["total_taxes"]),
            "total_deductions": float(self.record["total_deductions"]),
            "hours_units": float(self.record["hours_units"]),
            "source_page": self.source_page,
            "checks": self.checks,
        }


def _decimal(value: str | int | float | Decimal | None) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise IngestionError(f"Could not parse numeric value: {value!r}") from exc


def _money(value: str | int | float | Decimal | None) -> Decimal:
    return _decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _iso_date(value: str) -> str:
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError as exc:
        raise IngestionError(f"Could not parse date: {value!r}") from exc


def _capture_date(text: str, label: str) -> str:
    match = re.search(
        rf"{re.escape(label)}\s*:?\s*(\d{{1,2}}/\d{{1,2}}/\d{{4}})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise IngestionError(f"Missing required field: {label}")
    return _iso_date(match.group(1))


def _current_component(text: str, *labels: str) -> Decimal:
    for label in labels:
        match = re.search(
            rf"\b{re.escape(label)}\s+({NUMBER_PATTERN})",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if match:
            return _money(match.group(1))
    return Decimal("0.00")


def _statement_amounts(text: str) -> tuple[Decimal, Decimal]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        normalized = line.lower()
        if "check amount" in normalized and "gross pay" in normalized and "net pay" in normalized:
            for candidate in lines[index + 1 : index + 4]:
                amounts = re.findall(r"\$[\d,]+\.\d{2}", candidate)
                if len(amounts) >= 3:
                    return _money(amounts[-2]), _money(amounts[-1])
    raise IngestionError("Could not locate gross and net pay amounts.")


def _reported_totals(text: str) -> tuple[Decimal, Decimal, Decimal]:
    for line in text.splitlines():
        if line.strip().lower().startswith("total:") and line.lower().count("total:") >= 3:
            segments = re.split(r"Total:", line, flags=re.IGNORECASE)[1:]
            parsed = [
                [_decimal(value) for value in re.findall(NUMBER_PATTERN, segment)]
                for segment in segments
            ]
            if len(parsed) >= 3 and parsed[0] and parsed[1] and parsed[2]:
                return parsed[0][0], _money(parsed[1][0]), _money(parsed[2][0])
    raise IngestionError("Could not locate statement hours, tax, and deduction totals.")


def _earning_lines(text: str, label: str) -> list[tuple[Decimal, Decimal, Decimal]]:
    matches = re.findall(
        rf"^{re.escape(label)}(?:-[A-Za-z0-9]+)?\s+({NUMBER_PATTERN})\s+({NUMBER_PATTERN})\s+({NUMBER_PATTERN})",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return [(_decimal(rate), _decimal(hours), _money(dollars)) for rate, hours, dollars in matches]


def _sum_earning_lines(
    lines: Iterable[tuple[Decimal, Decimal, Decimal]],
) -> tuple[Decimal, Decimal, Decimal]:
    rows = list(lines)
    if not rows:
        return Decimal("0"), Decimal("0"), Decimal("0.00")
    total_hours = sum((row[1] for row in rows), Decimal("0"))
    total_pay = sum((row[2] for row in rows), Decimal("0.00"))
    unique_rates = {row[0] for row in rows}
    effective_rate = (
        rows[0][0]
        if len(unique_rates) == 1
        else (total_pay / total_hours if total_hours else rows[0][0])
    )
    return effective_rate.quantize(Decimal("0.0001")), total_hours, _money(total_pay)


def _tips_from_detail(text: str) -> Decimal:
    section_match = re.search(
        r"Non-Paid Earnings(.*?)(?:Employer Contributions|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not section_match:
        return Decimal("0.00")
    values = re.findall(
        rf"^Tips\s+({NUMBER_PATTERN})",
        section_match.group(1),
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return _money(sum((_decimal(value) for value in values), Decimal("0")))


def _bonus_from_detail(text: str) -> Decimal:
    values = re.findall(
        rf"^Bonus\s+({NUMBER_PATTERN})",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return _money(sum((_decimal(value) for value in values), Decimal("0")))


def _fallback_summary_earning(
    text: str, label: str
) -> tuple[Decimal, Decimal, Decimal]:
    rows = _earning_lines(text, label)
    return _sum_earning_lines(rows)


def parse_statement_text(
    summary_text: str,
    detail_text: str = "",
    source_page: int = 1,
) -> ParsedStatement:
    """Parse and reconcile one statement-summary page plus its optional detail page."""

    if "Statement of Earnings" not in summary_text:
        raise IngestionError("The selected page is not a supported statement of earnings.")

    pay_date = _capture_date(summary_text, "Check Date")
    period_begin = _capture_date(summary_text, "Period Begin")
    period_end = _capture_date(summary_text, "Period End")
    gross_pay, net_pay = _statement_amounts(summary_text)
    reported_hours, reported_taxes, reported_deductions = _reported_totals(summary_text)

    has_detail = "Employee Pay Details" in detail_text
    detail_source = detail_text if has_detail else summary_text
    regular_rate, regular_hours, regular_pay = _sum_earning_lines(
        [
            *_earning_lines(detail_source, "Regular"),
            *_earning_lines(detail_source, "Tip Reg"),
        ]
    )
    overtime_rate, overtime_hours, overtime_pay = _sum_earning_lines(
        _earning_lines(detail_source, "Overtime")
    )
    bonus_pay = (
        _bonus_from_detail(detail_text)
        if has_detail
        else _current_component(summary_text, "Bonus")
    )

    if not has_detail and regular_pay == 0 and gross_pay:
        regular_rate, regular_hours, regular_pay = _fallback_summary_earning(
            summary_text, "Regular"
        )
    if not has_detail and overtime_pay == 0:
        overtime_rate, overtime_hours, overtime_pay = _fallback_summary_earning(
            summary_text, "Overtime"
        )

    reported_tips = _tips_from_detail(detail_text)
    if reported_tips == 0:
        reported_tips = _current_component(summary_text, "Tips")

    social_security_tax = _current_component(summary_text, "SOC SEC EE")
    medicare_tax = _current_component(summary_text, "MED EE")
    federal_withholding = _current_component(summary_text, "FEDERAL WH")
    mississippi_withholding = _current_component(summary_text, "MISSISSIPPI WH")
    roth_401k = _current_component(summary_text, "Roth 401K")
    dental_insurance = _current_component(summary_text, "Dental Ins", "Dental Insurance")
    health_insurance = _current_component(summary_text, "Health Ins", "Health Insurance")

    total_taxes = _money(
        social_security_tax
        + medicare_tax
        + federal_withholding
        + mississippi_withholding
    )
    total_deductions = _money(roth_401k + dental_insurance + health_insurance)
    calculated_net = _money(gross_pay - total_taxes - total_deductions)
    paid_detail_gross = _money(regular_pay + overtime_pay + bonus_pay)
    calculated_hours = regular_hours + overtime_hours

    differences = {
        "net_difference": _money(calculated_net - net_pay),
        "tax_difference": _money(total_taxes - reported_taxes),
        "deduction_difference": _money(total_deductions - reported_deductions),
        "gross_difference": _money(paid_detail_gross - gross_pay),
        "hours_difference": (calculated_hours - reported_hours).quantize(Decimal("0.01")),
    }
    failures = {
        name: value
        for name, value in differences.items()
        if abs(value) > TOLERANCE
    }
    if failures:
        readable = ", ".join(f"{name}={value}" for name, value in failures.items())
        raise IngestionError(
            "Statement failed reconciliation and was not added: " + readable
        )

    pay_type_match = re.search(
        r"Pay Type:\s*([A-Za-z]+)",
        summary_text,
        flags=re.IGNORECASE,
    )
    pay_type = pay_type_match.group(1).strip() if pay_type_match else "Hourly"

    record: dict[str, object] = {
        "pay_date": pay_date,
        "period_begin": period_begin,
        "period_end": period_end,
        "year": int(pay_date[:4]),
        "pay_type": pay_type,
        "payment_type": (
            "Voucher / Direct Deposit"
            if re.search(r"\bVoucher Id\b", summary_text, flags=re.IGNORECASE)
            else "Check"
        ),
        "gross_pay": gross_pay,
        "total_taxes": total_taxes,
        "total_deductions": total_deductions,
        "calculated_net": calculated_net,
        "net_pay": net_pay,
        "hours_units": reported_hours,
        "regular_rate": regular_rate,
        "regular_hours": regular_hours,
        "regular_pay": regular_pay,
        "overtime_rate": overtime_rate,
        "overtime_hours": overtime_hours,
        "overtime_pay": overtime_pay,
        "bonus_pay": bonus_pay,
        "reported_tips": reported_tips,
        "social_security_tax": social_security_tax,
        "medicare_tax": medicare_tax,
        "federal_withholding": federal_withholding,
        "mississippi_withholding": mississippi_withholding,
        "roth_401k": roth_401k,
        "dental_insurance": dental_insurance,
        "health_insurance": health_insurance,
    }
    checks = {
        "status": "OK",
        **{name: float(value) for name, value in differences.items()},
    }
    return ParsedStatement(record=record, checks=checks, source_page=source_page)


def parse_pdf(pdf_path: str | Path) -> list[ParsedStatement]:
    """Extract all supported statement-summary pages from a PDF."""

    path = Path(pdf_path)
    if not path.is_file():
        raise IngestionError(f"PDF not found: {path}")
    if path.suffix.lower() != ".pdf":
        raise IngestionError("Only PDF files are supported.")

    statements: list[ParsedStatement] = []
    try:
        with pdfplumber.open(path) as document:
            page_texts = [page.extract_text() or "" for page in document.pages]
    except Exception as exc:
        raise IngestionError("The PDF could not be opened or read.") from exc

    for index, text in enumerate(page_texts):
        if "Statement of Earnings" not in text:
            continue
        detail_text = ""
        if index + 1 < len(page_texts) and "Employee Pay Details" in page_texts[index + 1]:
            detail_text = page_texts[index + 1]
        statements.append(parse_statement_text(text, detail_text, source_page=index + 1))

    if not statements:
        raise IngestionError(
            "No supported statement-of-earnings pages were found in this PDF."
        )
    return statements


def _format_csv_value(field: str, value: object) -> str | int:
    if field == "year":
        return int(value)
    if field not in DECIMAL_FIELDS:
        return str(value)
    decimal_value = _decimal(value)
    if field in MONEY_FIELDS:
        return f"{_money(decimal_value):.2f}"
    if field in {"regular_rate", "overtime_rate"}:
        return f"{decimal_value.quantize(Decimal('0.0001')):.4f}".rstrip("0").rstrip(".")
    return f"{decimal_value.quantize(Decimal('0.01')):.2f}".rstrip("0").rstrip(".")


def _duplicate_key(row: dict[str, object]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("pay_date", "")),
        str(row.get("period_begin", "")),
        str(row.get("period_end", "")),
        f"{_money(row.get('gross_pay')):.2f}",
        f"{_money(row.get('net_pay')):.2f}",
    )


def append_statements(
    csv_path: str | Path,
    statements: list[ParsedStatement],
    *,
    create_backup: bool = True,
) -> dict[str, object]:
    """Append nonduplicate statements atomically while preserving the CSV schema."""

    path = Path(csv_path)
    existing_rows: list[dict[str, str]] = []
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != CSV_FIELDS:
                raise IngestionError(
                    "The pay-history CSV schema does not match the ingestion schema."
                )
            existing_rows = list(reader)

    existing_keys = {_duplicate_key(row) for row in existing_rows}
    additions: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    seen_in_upload: set[tuple[str, str, str, str, str]] = set()

    for statement in statements:
        key = _duplicate_key(statement.record)
        if key in existing_keys or key in seen_in_upload:
            results.append(statement.public_summary("duplicate"))
            continue
        seen_in_upload.add(key)
        additions.append(statement.record)
        results.append(statement.public_summary("added"))

    if additions:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and create_backup:
            backup_dir = path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            shutil.copy2(path, backup_dir / f"{path.stem}-{stamp}{path.suffix}")

        combined: list[dict[str, object]] = [*existing_rows, *additions]
        combined.sort(key=lambda row: str(row["pay_date"]))
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
                writer.writeheader()
                for row in combined:
                    writer.writerow(
                        {
                            field: _format_csv_value(field, row.get(field, ""))
                            for field in CSV_FIELDS
                        }
                    )
            with open(temporary_name, "r", encoding="utf-8", newline="") as stream:
                verification_reader = csv.DictReader(stream)
                verification_rows = list(verification_reader)
            if verification_reader.fieldnames != CSV_FIELDS or len(verification_rows) != len(
                combined
            ):
                raise IngestionError(
                    "The staged CSV failed write verification; the live CSV was not changed."
                )
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    return {
        "status": "added" if additions else "duplicate",
        "added": len(additions),
        "duplicates": len(statements) - len(additions),
        "total_records": len(existing_rows) + len(additions),
        "rows": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract supported paystub PDFs and optionally append them to pay history."
    )
    parser.add_argument("pdf", type=Path, help="Paystub PDF to ingest")
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path(__file__).parent / "data" / "paystubs.csv",
        help="Destination pay-history CSV",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append nonduplicate statements; otherwise perform a dry run",
    )
    arguments = parser.parse_args()

    try:
        statements = parse_pdf(arguments.pdf)
        if arguments.append:
            result = append_statements(arguments.csv, statements)
        else:
            result = {
                "status": "dry-run",
                "added": 0,
                "duplicates": 0,
                "total_records": None,
                "rows": [
                    statement.public_summary("validated") for statement in statements
                ],
            }
        print(json.dumps(result, indent=2))
        return 0
    except IngestionError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
