import csv
import tempfile
import unittest
from pathlib import Path

from ingestion import (
    CSV_FIELDS,
    IngestionError,
    _other_pay_from_summary,
    append_statements,
    parse_statement_text,
)


SUMMARY_TEXT = """
Statement of Earnings For: Example Employee
Period Begin: 7/6/2026 Period End: 7/19/2026 Check Date: 7/24/2026 Pay Type: Hourly
Voucher Id Check Amount Gross Pay Net Pay Check Message
V0000000 $0.00 $894.04 $743.02
EARNINGS TAXES DEDUCTIONS
Regular 11.2500 78.27 880.54 1,191.42 13,103.49 SOC SEC EE 60.75 953.24 Roth 401K 20.00 300.00
Overtime 16.8750 0.80 13.50 53.63 869.83 MED EE 14.21 222.94
Tips 85.82 0.00 1,401.58 FEDERAL WH 36.06 612.12
MISSISSIPPI WH 20.00 330.00
Total: 79.07 979.86 1,245.05 15,374.90 Total: 131.02 2,118.30 Total: 20.00 300.00
"""

DETAIL_TEXT = """
Employee Pay Details
For Pay Period: 7/6/2026 - 7/19/2026
Pay Date: 7/24/2026
Regular 11.2500 40.00 450.00 1 00 Instore
Regular 11.2500 38.27 430.54 2 00 Instore
Overtime 16.8750 0.80 13.50 1 00 Instore
79.07 894.04
Non-Paid Earnings
Tips 56.50 00 Instore
Tips 29.32 00 Instore
85.82
Employer Contributions and Other Memo Calculations
"""

TIPPED_SUMMARY_TEXT = """
Statement of Earnings For: Example Employee
Period Begin: 7/20/2026 Period End: 8/2/2026 Check Date: 8/7/2026 Pay Type: Hourly
Voucher Id Check Amount Gross Pay Net Pay Check Message
V0000001 $0.00 $228.41 $200.55
EARNINGS TAXES DEDUCTIONS
Regular-DV 9.0000 15.14 136.26 126.26 1,136.34 SOC SEC EE 22.58 210.41
Tips 135.76 0.00 1,332.21 MED EE 5.28 49.21
Tip Reg-DR 5.0000 18.43 92.15 173.60 868.00
Regular 0.00 6.35 57.15
Total: 33.57 364.17 306.21 3,393.70 Total: 27.86 259.62 Total: 0.00 0.00
"""

TIPPED_DETAIL_TEXT = """
Employee Pay Details
For Pay Period: 7/20/2026 - 8/2/2026
Pay Date: 8/7/2026
Regular-DV 9.0000 7.85 70.65 2 00 Instore
Regular-DV 9.0000 7.29 65.61 1 00 Instore
Tip Reg-DR 5.0000 9.30 46.50 1 05 Driver
Tip Reg-DR 5.0000 9.13 45.65 2 05 Driver
33.57 228.41
Non-Paid Earnings
Tips 59.25 05 Driver
Tips 76.51 05 Driver
135.76
"""

OTHER_EARNING_SUMMARY_TEXT = """
Statement of Earnings For: Example Employee
Period Begin: 8/3/2026 Period End: 8/16/2026 Check Date: 8/21/2026 Pay Type: Hourly
Voucher Id Check Amount Gross Pay Net Pay Check Message
V0000002 $0.00 $922.15 $779.37
EARNINGS TAXES DEDUCTIONS
Regular 11.2500 77.45 871.31 1,345.08 14,832.16 SOC SEC EE 58.21 1,068.17 Roth 401K 20.00 340.00
Overtime 16.8750 0.05 0.84 53.68 870.67 MED EE 13.61 249.81
Other 11.2500 50.00 0.00 50.00 FEDERAL WH 31.96 673.63
Tips 16.71 0.00 1,475.64 MISSISSIPPI WH 19.00 367.00
Total: 77.50 938.86 1,398.76 17,228.47 Total: 122.78 2,358.61 Total: 20.00 340.00
"""

OTHER_EARNING_DETAIL_TEXT = """
Employee Pay Details
For Pay Period: 8/3/2026 - 8/16/2026
Pay Date: 8/21/2026
Regular 11.2500 40.00 450.00 1 00 Instore
Regular 11.2500 37.45 421.31 2 00 Instore
Overtime 16.8750 0.05 0.84 1 00 Instore
Other 11.2500 50.00 00 Instore
77.50 922.15
Non-Paid Earnings
Tips 10.44 00 Instore
Tips 6.27 00 Instore
16.71
"""

ZERO_OTHER_SUMMARY_TEXT = """
Statement of Earnings For: Example Employee
Period Begin: 8/31/2026 Period End: 9/13/2026 Check Date: 9/18/2026 Pay Type: Hourly
Voucher Id Check Amount Gross Pay Net Pay Check Message
V0000003 $0.00 $802.58 $663.25
EARNINGS TAXES DEDUCTIONS
Regular 11.2500 71.34 802.58 1,493.19 16,498.40 SOC SEC EE 57.35 1,180.90 Roth 401K 20.00 380.00
Tips 122.36 0.00 1,627.63 MED EE 13.41 276.18
Overtime 0.00 53.68 870.67 FEDERAL WH 30.57 731.61
MISSISSIPPI WH 18.00 402.00
Other 0.00 0.00 50.00
Total: 71.34 924.94 1,546.87 19,046.70 Total: 119.33 2,590.69 Total: 20.00 380.00
"""

ZERO_OTHER_DETAIL_TEXT = """
Employee Pay Details
For Pay Period: 8/31/2026 - 9/13/2026
Pay Date: 9/18/2026
Regular 11.2500 38.38 431.78 00 Instore
Regular 11.2500 32.96 370.80 00 Instore
71.34 802.58
Non-Paid Earnings
Tips 68.11 00 Instore
Tips 54.25 00 Instore
122.36
Employer Contributions and Other Memo Calculations
"""


class IngestionTests(unittest.TestCase):
    def test_statement_extracts_and_reconciles(self):
        statement = parse_statement_text(SUMMARY_TEXT, DETAIL_TEXT)
        record = statement.record

        self.assertEqual(record["pay_date"], "2026-07-24")
        self.assertEqual(float(record["gross_pay"]), 894.04)
        self.assertEqual(float(record["net_pay"]), 743.02)
        self.assertEqual(float(record["regular_hours"]), 78.27)
        self.assertEqual(float(record["overtime_hours"]), 0.80)
        self.assertEqual(float(record["reported_tips"]), 85.82)
        self.assertEqual(statement.checks["status"], "OK")

    def test_tipped_role_codes_reconcile_paid_and_nonpaid_earnings(self):
        statement = parse_statement_text(TIPPED_SUMMARY_TEXT, TIPPED_DETAIL_TEXT)
        record = statement.record

        self.assertEqual(float(record["gross_pay"]), 228.41)
        self.assertEqual(float(record["regular_hours"]), 33.57)
        self.assertEqual(float(record["regular_pay"]), 228.41)
        self.assertEqual(float(record["reported_tips"]), 135.76)
        self.assertEqual(statement.checks["gross_difference"], 0.0)
        self.assertEqual(statement.checks["hours_difference"], 0.0)

    def test_flat_other_earning_reconciles_without_counting_it_as_hours(self):
        statement = parse_statement_text(
            OTHER_EARNING_SUMMARY_TEXT, OTHER_EARNING_DETAIL_TEXT
        )
        record = statement.record

        self.assertEqual(float(record["gross_pay"]), 922.15)
        self.assertEqual(float(record["regular_hours"]), 77.45)
        self.assertEqual(float(record["overtime_hours"]), 0.05)
        self.assertEqual(float(record["bonus_pay"]), 50.00)
        self.assertEqual(statement.checks["gross_difference"], 0.0)
        self.assertEqual(statement.checks["hours_difference"], 0.0)

    def test_zero_current_other_does_not_import_year_to_date_pay(self):
        statement = parse_statement_text(ZERO_OTHER_SUMMARY_TEXT, ZERO_OTHER_DETAIL_TEXT)
        record = statement.record

        self.assertEqual(float(record["gross_pay"]), 802.58)
        self.assertEqual(float(record["net_pay"]), 663.25)
        self.assertEqual(float(record["bonus_pay"]), 0.0)
        self.assertEqual(float(record["reported_tips"]), 122.36)
        self.assertEqual(float(record["hours_units"]), 71.34)
        self.assertEqual(statement.checks["gross_difference"], 0.0)
        self.assertEqual(statement.checks["hours_difference"], 0.0)

    def test_other_earnings_accept_populated_column_variants(self):
        cases = (
            ("Other 12.00 70.00", "12.00"),
            ("Other 0.00 0.00 50.00", "0.00"),
            ("Other 11.2500 50.00 0.00 50.00 FEDERAL WH 30.57 731.61", "50.00"),
            ("Other 11.2500 2.00 22.50 4.00 45.00", "22.50"),
        )
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertEqual(str(_other_pay_from_summary(row)), expected)

        with self.assertRaisesRegex(IngestionError, "Other earnings amount"):
            _other_pay_from_summary("Other unavailable")

    def test_unknown_deduction_is_preserved_as_other_when_totals_balance(self):
        summary = (
            SUMMARY_TEXT
            .replace("$743.02", "$715.14")
            .replace("Roth 401K 20.00 300.00", "Roth 401K 20.00 300.00 Vision Plan 27.88 27.88")
            .replace("Total: 20.00 300.00", "Total: 47.88 327.88")
        )

        statement = parse_statement_text(summary, DETAIL_TEXT)

        self.assertEqual(float(statement.record["total_deductions"]), 47.88)
        self.assertEqual(float(statement.record["roth_401k"]), 20.00)
        self.assertEqual(float(statement.record["other_deductions"]), 27.88)
        self.assertEqual(statement.checks["net_difference"], 0.0)
        self.assertEqual(statement.checks["deduction_difference"], 0.0)

    def test_traditional_401k_label_is_categorized_as_retirement(self):
        summary = (
            SUMMARY_TEXT
            .replace("$743.02", "$724.34")
            .replace("Roth 401K 20.00 300.00", "401K 38.68 675.12")
            .replace("Total: 20.00 300.00", "Total: 38.68 675.12")
        )

        statement = parse_statement_text(summary, DETAIL_TEXT)

        self.assertEqual(float(statement.record["roth_401k"]), 38.68)
        self.assertEqual(float(statement.record["other_deductions"]), 0.0)
        self.assertEqual(statement.checks["net_difference"], 0.0)

    def test_known_deductions_cannot_exceed_reported_total(self):
        summary = SUMMARY_TEXT.replace(
            "Total: 20.00 300.00", "Total: 10.00 300.00"
        )

        with self.assertRaisesRegex(IngestionError, "deduction_difference=10.00"):
            parse_statement_text(summary, DETAIL_TEXT)

    def test_append_is_atomic_and_duplicate_safe(self):
        statement = parse_statement_text(SUMMARY_TEXT, DETAIL_TEXT)
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "paystubs.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as stream:
                csv.DictWriter(stream, fieldnames=CSV_FIELDS).writeheader()

            first = append_statements(csv_path, [statement], create_backup=False)
            bytes_before_duplicate = csv_path.read_bytes()
            second = append_statements(csv_path, [statement], create_backup=False)
            bytes_after_duplicate = csv_path.read_bytes()

            self.assertEqual(first["added"], 1)
            self.assertEqual(second["added"], 0)
            self.assertEqual(second["duplicates"], 1)
            self.assertEqual(bytes_before_duplicate, bytes_after_duplicate)
            with csv_path.open("r", encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)

    def test_append_upgrades_legacy_csv_without_other_deductions_column(self):
        statement = parse_statement_text(SUMMARY_TEXT, DETAIL_TEXT)
        legacy_fields = [field for field in CSV_FIELDS if field != "other_deductions"]
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "paystubs.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as stream:
                csv.DictWriter(stream, fieldnames=legacy_fields).writeheader()

            append_statements(csv_path, [statement], create_backup=False)

            with csv_path.open("r", encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)
            self.assertEqual(reader.fieldnames, CSV_FIELDS)
            self.assertEqual(rows[0]["other_deductions"], "0.00")


if __name__ == "__main__":
    unittest.main()
