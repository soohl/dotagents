#!/usr/bin/env python3
import json
import subprocess
import sys
import unittest
from pathlib import Path

W = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(W))
from planwise import PlanError, parse_tasks, plan, to_dot


class Tests(unittest.TestCase):
    def test_validation(self):
        invalid = [
            {}, {"tasks": [], "extra": 1}, {"tasks": "x"},
            {"tasks": [{"id": "a", "duration": True}]},
            {"tasks": [{"id": "a", "duration": 0}]},
            {"tasks": [{"id": "a", "duration": 1, "extra": 1}]},
            {"tasks": [{"id": "a", "duration": 1}, {"id": "a", "duration": 2}]},
            {"tasks": [{"id": "a", "duration": 1, "depends_on": ["a"]}]},
            {"tasks": [{"id": "a", "duration": 1, "depends_on": ["missing"]}]},
            {"tasks": [{"id": "a", "duration": 1, "depends_on": ["b", "b"]},
                       {"id": "b", "duration": 1}]},
        ]
        for document in invalid:
            with self.subTest(document=document), self.assertRaises(PlanError):
                parse_tasks(document)
        cycle = {"tasks": [
            {"id": "a", "duration": 1, "depends_on": ["b"]},
            {"id": "b", "duration": 1, "depends_on": ["a"]},
        ]}
        try:
            tasks = parse_tasks(cycle)
        except PlanError:
            pass
        else:
            with self.assertRaises(PlanError):
                plan(tasks)

    def test_order_and_waves(self):
        tasks = parse_tasks({"tasks": [
            {"id": "root", "duration": 1},
            {"id": "z", "duration": 1, "depends_on": ["root"], "priority": 3},
            {"id": "a", "duration": 1, "depends_on": ["root"], "priority": 3},
            {"id": "m", "duration": 1, "depends_on": ["root"], "priority": 1},
            {"id": "end", "duration": 1, "depends_on": ["z", "a", "m"]},
        ]})
        result = plan(tasks, jobs=2)
        self.assertEqual(result["topological_order"], ["root", "a", "z", "m", "end"])
        self.assertEqual(result["waves"], [["root"], ["a", "z"], ["m"], ["end"]])
        for bad in (0, -1, True, 1.5):
            with self.subTest(bad=bad), self.assertRaises(PlanError):
                plan(tasks, bad)

    def test_critical_paths(self):
        tasks = parse_tasks({"tasks": [
            {"id": "b", "duration": 2},
            {"id": "a", "duration": 2},
            {"id": "end", "duration": 1, "depends_on": ["b", "a"]},
        ]})
        result = plan(tasks, 3)
        self.assertEqual(result["critical_path"], ["a", "end"])
        self.assertEqual(result["critical_duration"], 3)
        self.assertEqual(plan([], 1), {
            "topological_order": [], "waves": [],
            "critical_path": [], "critical_duration": 0,
        })

    def test_dot(self):
        tasks = parse_tasks({"tasks": [
            {"id": 'a"x', "duration": 1},
            {"id": "b", "duration": 1, "depends_on": ['a"x']},
            {"id": "solo", "duration": 1},
        ]})
        dot = to_dot(tasks)
        self.assertIn('"a\\"x";', dot)
        self.assertIn('"solo";', dot)
        self.assertIn('"a\\"x" -> "b";', dot)
        self.assertEqual(dot, to_dot(list(reversed(tasks))))

    def test_cli(self):
        normal = subprocess.run(
            [sys.executable, "-m", "planwise", "example.json", "--jobs", "2"],
            cwd=W, text=True, capture_output=True,
        )
        self.assertEqual(normal.returncode, 0, normal.stderr)
        self.assertEqual(json.loads(normal.stdout)["critical_duration"], 7)
        stdin = subprocess.run(
            [sys.executable, "-m", "planwise", "-", "--dot"],
            cwd=W, input='{"tasks":[{"id":"x","duration":1}]}',
            text=True, capture_output=True,
        )
        self.assertEqual(stdin.returncode, 0, stdin.stderr)
        self.assertIn('"x";', stdin.stdout)
        bad = subprocess.run(
            [sys.executable, "-m", "planwise", "-"],
            cwd=W, input="bad", text=True, capture_output=True,
        )
        self.assertEqual(bad.returncode, 2)
        self.assertNotIn("Traceback", bad.stderr)


def main():
    checks = []
    for name in unittest.defaultTestLoader.getTestCaseNames(Tests):
        result = unittest.TextTestRunner(stream=sys.stderr).run(Tests(name))
        checks.append({"name": name, "passed": result.wasSuccessful(), "points": 4})
    passed = sum(x["passed"] for x in checks)
    score = sum(x["points"] for x in checks if x["passed"])
    maximum = sum(x["points"] for x in checks)
    print(json.dumps({"passed": passed == len(checks), "score": score,
                      "max_score": maximum, "checks": checks}))
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
