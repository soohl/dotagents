#!/usr/bin/env python3
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

W = Path(sys.argv[1]).resolve()


def check(name, passed, detail="", points=2):
    return {"name": name, "passed": bool(passed), "detail": detail, "points": points}


def official_sources(sources):
    paths = {(urlparse(url).hostname, urlparse(url).path.rstrip("/")) for url in sources
             if isinstance(url, str) and url.startswith("https://")}
    return (
        ("peps.python.org", "/pep-0621") in paths
        and ("packaging.python.org", "/en/latest/specifications/name-normalization") in paths
    )


def main():
    try:
        data = json.loads((W / "findings.json").read_text())
        loaded = isinstance(data, dict)
        error = ""
    except Exception as exc:
        data, loaded, error = {}, False, str(exc)
    checks = [check("valid findings.json", loaded, error)]
    checks.append(check("PEP 621 identity", data.get("pep_621") == {
        "title": "Storing project metadata in pyproject.toml",
        "status": "Final",
        "created": "2020-06-22",
    }))
    checks.append(check("name normalization", data.get("normalization") == {
        "input": "Friendly_Bard.baz",
        "output": "friendly-bard-baz",
        "replacement_pattern": "[-_.]+",
        "replacement": "-",
    }))
    sources = data.get("sources", [])
    checks.append(check("official citations", isinstance(sources, list)
                        and len(sources) >= 2 and official_sources(sources)))
    score = sum(item["points"] for item in checks if item["passed"])
    maximum = sum(item["points"] for item in checks)
    passed = all(item["passed"] for item in checks)
    print(json.dumps({"passed": passed, "score": score,
                      "max_score": maximum, "checks": checks}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
