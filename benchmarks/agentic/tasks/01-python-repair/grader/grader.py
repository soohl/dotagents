#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

W = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(W))
from ledgerlite import LedgerError, parse_amount, summarize


class Tests(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(parse_amount(" 1.005 "), Decimal("1.01"))
        self.assertEqual(parse_amount(2), Decimal("2.00"))
        for bad in ("", "NaN", "Infinity", "-.01", True, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_amount(bad)

    def test_summary_and_validation(self):
        result = summarize([
            {"account": "z", "kind": "debit", "amount": "1"},
            {"account": "a", "kind": "credit", "amount": "2.5"},
            {"account": "a", "kind": "debit", "amount": ".25"},
        ])
        self.assertEqual(list(result), ["a", "z"])
        self.assertEqual(result["a"], {"debit": "0.25", "credit": "2.50", "net": "2.25"})
        self.assertEqual(result["z"], {"debit": "1.00", "credit": "0.00", "net": "-1.00"})
        invalid = [
            [{"account": "", "kind": "credit", "amount": 1}],
            [{"account": 4, "kind": "credit", "amount": 1}],
            [{"account": "a", "kind": "refund", "amount": 1}],
            [{"account": "a", "kind": "credit", "amount": -1}],
        ]
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises((LedgerError, ValueError)):
                summarize(rows)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text(json.dumps([
                {"account": "b", "kind": "credit", "amount": 2},
                {"account": "a", "kind": "debit", "amount": 1},
            ]))
            env = {**os.environ, "PYTHONPATH": str(W)}
            result = subprocess.run(
                [sys.executable, "-m", "ledgerlite", str(path)],
                cwd=W, env=env, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(json.loads(result.stdout)), ["a", "b"])
            path.write_text("not-json")
            bad = subprocess.run(
                [sys.executable, "-m", "ledgerlite", str(path)],
                cwd=W, env=env, text=True, capture_output=True,
            )
            self.assertEqual(bad.returncode, 2)
            self.assertNotIn("Traceback", bad.stderr)
            usage = subprocess.run(
                [sys.executable, "-m", "ledgerlite"],
                cwd=W, env=env, text=True, capture_output=True,
            )
            self.assertEqual(usage.returncode, 2)


def main():
    checks = []
    for name in unittest.defaultTestLoader.getTestCaseNames(Tests):
        result = unittest.TextTestRunner(stream=sys.stderr).run(Tests(name))
        checks.append({"name": name, "passed": result.wasSuccessful(), "points": 2})
    score = sum(x["passed"] for x in checks)
    weighted = sum(x["points"] for x in checks if x["passed"])
    maximum = sum(x["points"] for x in checks)
    print(json.dumps({"passed": score == len(checks), "score": weighted,
                      "max_score": maximum, "checks": checks}))
    return 0 if score == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
