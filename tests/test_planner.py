import json
import tempfile
import unittest
from pathlib import Path

from server import load_planner, normalize_planner, save_planner


class PlannerPersistenceTests(unittest.TestCase):
    def test_normalize_accepts_complete_planner(self):
        planner = normalize_planner(
            {
                "allocations": {
                    "mode": "amount",
                    "values": {
                        "needs": 400,
                        "savings": 150,
                        "debt": 100,
                        "flexible": 75,
                    },
                },
                "expenses": [
                    {
                        "id": "rent",
                        "name": "Rent",
                        "category": "Housing",
                        "amount": 800,
                        "frequency": "monthly",
                    }
                ],
                "goals": [
                    {
                        "id": "emergency",
                        "name": "Emergency fund",
                        "target": 5000,
                        "saved": 1200,
                        "date": "2027-06-01",
                    }
                ],
            }
        )

        self.assertEqual(planner["allocations"]["mode"], "amount")
        self.assertEqual(planner["expenses"][0]["amount"], 800)
        self.assertEqual(planner["goals"][0]["saved"], 1200)

    def test_save_is_atomic_and_loads_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "planner.json"
            expected = save_planner(
                {
                    "allocations": {
                        "mode": "percent",
                        "values": {"needs": 45, "savings": 25, "debt": 15, "flexible": 15},
                    },
                    "expenses": [],
                    "goals": [],
                },
                path,
            )

            self.assertEqual(load_planner(path), expected)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_invalid_frequency_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "frequency"):
            normalize_planner(
                {
                    "expenses": [
                        {
                            "name": "Rent",
                            "amount": 500,
                            "frequency": "sometimes",
                        }
                    ]
                }
            )


if __name__ == "__main__":
    unittest.main()
