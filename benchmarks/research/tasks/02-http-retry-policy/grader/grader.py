#!/usr/bin/env python3
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

W = Path(sys.argv[1]).resolve()


def check(name, passed, detail="", points=1):
    return {"name": name, "passed": bool(passed), "detail": detail, "points": points}


def cited(sources, rfc):
    return any(
        isinstance(url, str)
        and url.startswith("https://")
        and urlparse(url).hostname in {"rfc-editor.org", "www.rfc-editor.org"}
        and f"rfc{rfc}" in urlparse(url).path.lower()
        for url in sources
    )


def main():
    try:
        data = json.loads((W / "policy.json").read_text())
        loaded, error = isinstance(data, dict), ""
    except Exception as exc:
        data, loaded, error = {}, False, str(exc)
    checks = [check("valid policy.json", loaded, error, 2)]
    checks.append(check("429 policy", data.get("status_429") == {
        "name": "Too Many Requests",
        "meaning": "rate limiting",
        "retry_after_requirement": "MAY",
    }, points=3))
    checks.append(check("Retry-After policy", data.get("retry_after") == {
        "forms": ["HTTP-date", "delay-seconds"],
        "delay_seconds_constraint": "non-negative decimal integer",
        "with_503_indicates": "expected service unavailability duration",
    }, points=4))
    checks.append(check("308 policy", data.get("status_308") == {
        "name": "Permanent Redirect",
        "permanent": True,
        "cacheable_by_default": True,
        "post_to_get_allowed": False,
    }, points=3))
    sources = data.get("sources", [])
    checks.append(check("three RFC citations", isinstance(sources, list)
                        and all(cited(sources, rfc) for rfc in (6585, 9110, 7538)),
                        points=2))
    score = sum(item["points"] for item in checks if item["passed"])
    maximum = sum(item["points"] for item in checks)
    passed = all(item["passed"] for item in checks)
    print(json.dumps({"passed": passed, "score": score,
                      "max_score": maximum, "checks": checks}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
