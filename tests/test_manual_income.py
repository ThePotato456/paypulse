import unittest

from server import normalize_manual_income


class ManualIncomeTests(unittest.TestCase):
    def test_benefit_deposit_defaults_gross_to_net(self):
        record = normalize_manual_income(
            {
                "income_type": "va-benefits",
                "source": "VA Disability",
                "payment_method": "direct-deposit",
                "income_frequency": "monthly",
                "pay_date": "2026-08-01",
                "net_pay": 1850.25,
            }
        )

        self.assertEqual(record["gross_pay"], 1850.25)
        self.assertEqual(record["net_pay"], 1850.25)
        self.assertEqual(record["hours_units"], 0.0)
        self.assertEqual(record["pay_type"], "Manual: VA Disability")
        self.assertEqual(record["payment_type"], "VA Disability · Direct deposit")
        self.assertEqual(record["income_frequency"], "monthly")

    def test_manual_paystub_keeps_optional_hours_and_withholding(self):
        record = normalize_manual_income(
            {
                "income_type": "paystub",
                "source": "Weekend payroll",
                "payment_method": "check",
                "pay_date": "2026-08-07",
                "period_begin": "2026-07-20",
                "period_end": "2026-08-02",
                "gross_pay": 1000,
                "total_taxes": 150,
                "total_deductions": 50,
                "net_pay": 800,
                "regular_rate": 20,
                "regular_hours": 40,
                "overtime_hours": 2,
            }
        )

        self.assertEqual(record["hours_units"], 42.0)
        self.assertEqual(record["overtime_rate"], 30.0)
        self.assertEqual(record["total_taxes"], 150.0)
        self.assertEqual(record["calculated_net"], 800.0)

    def test_manual_entry_rejects_unreconciled_amounts(self):
        with self.assertRaisesRegex(ValueError, "must equal the deposit amount"):
            normalize_manual_income(
                {
                    "income_type": "social-security",
                    "pay_date": "2026-08-03",
                    "gross_pay": 1000,
                    "total_taxes": 100,
                    "net_pay": 950,
                }
            )


if __name__ == "__main__":
    unittest.main()
