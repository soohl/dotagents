import unittest
from planwise import parse_tasks, plan


class Tests(unittest.TestCase):
    def test_basic(self):
        tasks = parse_tasks({"tasks": [
            {"id": "a", "duration": 2},
            {"id": "b", "duration": 3, "depends_on": ["a"]},
            {"id": "c", "duration": 1, "depends_on": ["a"], "priority": 2},
        ]})
        result = plan(tasks, jobs=2)
        self.assertEqual(result["topological_order"], ["a", "c", "b"])
        self.assertEqual(result["waves"], [["a"], ["c", "b"]])
        self.assertEqual(result["critical_path"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
