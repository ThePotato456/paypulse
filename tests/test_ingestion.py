import csv
import tempfile
import unittest
from pathlib import Path

from ingestion import CSV_FIELDS, append_statements, parse_statement_text


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


if __name__ == "__main__":
    unittest.main()
