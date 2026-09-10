import unittest
from decimal import Decimal

from ledgerlite import parse_amount, summarize


class Tests(unittest.TestCase):
    def test_rounding(self):
        self.assertEqual(parse_amount("1.005"), Decimal("1.01"))

    def test_summary(self):
        result = summarize([
            {"account": "cash", "kind": "credit", "amount": "10"},
            {"account": "cash", "kind": "debit", "amount": "3.25"},
        ])
        self.assertEqual(result["cash"]["net"], "6.75")


if __name__ == "__main__":
    unittest.main()
