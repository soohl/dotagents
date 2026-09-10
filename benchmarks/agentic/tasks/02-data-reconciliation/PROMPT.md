# Reconcile support operations exports

Analyze `data/cases.csv` and `data/details.json`. Write a reusable
standard-library-only `analyze.py`, run it, and create:

- `output/cases_clean.json`
- `output/team_summary.csv`
- `output/anomalies.json`
- `REPORT.md`

Rules:

1. Normalize case IDs by trimming and uppercasing.
2. CSV rows are snapshots. For duplicate IDs, retain the latest valid
   `updated_at`; ties use the later physical row.
3. Status after trim/lowercase must be `open`, `pending`, or `closed`.
4. A team and case ID must be non-empty after trimming.
5. JSON `minutes` is a non-negative integer; integer strings are accepted but
   booleans are not. `satisfaction` is an integer 1–5 or null; strings are not
   accepted.
6. Duplicate JSON detail IDs invalidate both duplicate rows and that case.
7. A clean case requires one valid retained snapshot and one valid, nonduplicate
   detail. Preserve the retained timestamp text exactly.
8. `cases_clean.json` is sorted by case ID. Keys are `case_id`, `team`,
   `status`, `updated_at`, `minutes`, `satisfaction`.
9. `team_summary.csv` has
   `team,cases,total_minutes,avg_satisfaction,closed_cases`, sorted by team.
   Average only non-null satisfaction and format it to two decimals or blank.
10. `anomalies.json` contains every invalid source row, both rows of duplicate
    detail IDs, and valid detail rows with no valid snapshot. Do not report a
    valid superseded snapshot or separately report a valid snapshot whose
    detail is invalid. Each entry has `source`, `row`, `case_id`, `reason`,
    sorted by source, row, case ID. Row numbers are one-based data positions.
11. `REPORT.md` states both input counts, clean count, anomaly count, and the
    team with the highest total minutes.

Do not modify `data/`. Independently verify generated artifacts before
finishing.
