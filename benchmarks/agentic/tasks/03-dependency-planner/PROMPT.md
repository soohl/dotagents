# Implement Planwise

Implement a dependency-aware planning package and CLI. Use only the Python
standard library.

Input:

```json
{"tasks":[{"id":"compile","duration":3,"depends_on":["generate"],"priority":2}]}
```

Contract:

- Document must contain only `tasks`, an array.
- Each task contains only `id`, `duration`, optional `depends_on`, and optional
  `priority`.
- ID: non-empty string. Duration: positive integer. Priority: integer default
  zero. Booleans are not integers here.
- Dependencies default to `[]` and are unique task-ID strings.
- Reject duplicate IDs, self/unknown dependencies, malformed fields, and cycles
  with `PlanError`.
- `plan(tasks, jobs)` returns:
  - `topological_order`: Kahn ordering; choose ready tasks by higher priority,
    then lexicographically smaller ID.
  - `waves`: choose up to `jobs` ready tasks per wave with the same ordering.
    Dependents may enter only a later wave than all dependencies.
  - `critical_path` and `critical_duration`: greatest summed-duration
    dependency path; ties choose the lexicographically smaller ID list.
- Empty task sets are valid. Jobs is a positive non-boolean integer.
- `to_dot(tasks)` returns deterministic Graphviz containing all nodes and
  dependency→dependent edges, safely escaping IDs.

CLI:

```sh
python3 -m planwise INPUT.json --jobs 2
cat INPUT.json | python3 -m planwise - --jobs 2
python3 -m planwise INPUT.json --dot
```

JSON output is deterministic. User errors exit 2 with concise stderr and no
traceback. Keep logic reusable, add meaningful tests, and verify all CLI forms.
