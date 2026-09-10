#!/usr/bin/env python3
import csv
import json
import re
import sys
from pathlib import Path

W = Path(sys.argv[1]).resolve()

CASES = [
    {"case_id": "C-001", "team": "Alpha", "status": "closed",
     "updated_at": "2026-07-03T12:00:00Z", "minutes": 35, "satisfaction": 5},
    {"case_id": "C-002", "team": "Beta", "status": "pending",
     "updated_at": "2026-07-01T10:00:00Z", "minutes": 20, "satisfaction": 4},
    {"case_id": "C-006", "team": "Gamma", "status": "closed",
     "updated_at": "2026-07-02T10:00:00Z", "minutes": 45, "satisfaction": None},
]
SUMMARY = [
    {"team": "Alpha", "cases": "1", "total_minutes": "35", "avg_satisfaction": "5.00", "closed_cases": "1"},
    {"team": "Beta", "cases": "1", "total_minutes": "20", "avg_satisfaction": "4.00", "closed_cases": "0"},
    {"team": "Gamma", "cases": "1", "total_minutes": "45", "avg_satisfaction": "", "closed_cases": "1"},
]
ANOMALIES = {
    ("cases.csv", 4, "C-003"), ("cases.csv", 6, "C-005"),
    ("cases.csv", 10, ""), ("cases.csv", 11, "C-008"),
    ("details.json", 3, "C-003"), ("details.json", 4, "C-004"),
    ("details.json", 5, "C-005"), ("details.json", 7, "C-007"),
    ("details.json", 8, "C-007"), ("details.json", 9, "C-008"),
    ("details.json", 10, "C-009"), ("details.json", 11, "C-010"),
    ("details.json", 12, ""),
}


def check(name, passed, detail="", points=1):
    return {"name": name, "passed": bool(passed), "detail": detail, "points": points}


def main():
    checks = []
    try:
        clean = json.loads((W / "output/cases_clean.json").read_text())
        checks.append(check("clean cases exact", clean == CASES, points=2))
    except Exception as error:
        checks.append(check("clean cases exact", False, str(error), points=2))
    try:
        with (W / "output/team_summary.csv").open(newline="") as handle:
            summary = list(csv.DictReader(handle))
        checks.append(check("team summary exact", summary == SUMMARY, points=2))
    except Exception as error:
        checks.append(check("team summary exact", False, str(error), points=2))
    try:
        anomalies = json.loads((W / "output/anomalies.json").read_text())
        keys = {(x["source"], x["row"], x["case_id"]) for x in anomalies}
        shape = all(list(x) == ["source", "row", "case_id", "reason"] and x["reason"] for x in anomalies)
        ordered = anomalies == sorted(anomalies, key=lambda x: (x["source"], x["row"], x["case_id"]))
        checks.append(check("anomaly coverage", keys == ANOMALIES, points=2))
        checks.append(check("anomaly shape/order", shape and ordered, points=2))
    except Exception as error:
        checks += [check("anomaly coverage", False, str(error), points=2), check("anomaly shape/order", False, str(error), points=2)]
    try:
        report = (W / "REPORT.md").read_text().lower()
        facts = (
            "gamma" in report
            and re.search(r"csv.{0,24}\b12\b", report)
            and re.search(r"json.{0,24}\b12\b", report)
            and re.search(r"clean.{0,24}\b3\b", report)
            and re.search(r"anomal.{0,24}\b13\b", report)
        )
        checks.append(check("report facts", bool(facts), points=2))
    except Exception as error:
        checks.append(check("report facts", False, str(error), points=2))
    checks.append(check("reusable script", (W / "analyze.py").is_file(), points=2))
    passed = sum(x["passed"] for x in checks)
    score = sum(x["points"] for x in checks if x["passed"])
    maximum = sum(x["points"] for x in checks)
    print(json.dumps({"passed": passed == len(checks), "score": score,
                      "max_score": maximum, "checks": checks}))
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
