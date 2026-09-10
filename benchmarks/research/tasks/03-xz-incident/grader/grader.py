#!/usr/bin/env python3
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

W = Path(sys.argv[1]).resolve()


def check(name, passed, detail="", points=1):
    return {"name": name, "passed": bool(passed), "detail": detail, "points": points}


def source_classes(sources):
    found = set()
    for url in sources:
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        parsed = urlparse(url)
        if parsed.hostname in {"openwall.com", "www.openwall.com"} and \
                "/lists/oss-security/2024/03/29/4" in parsed.path:
            found.add("disclosure")
        if parsed.hostname == "tukaani.org" and parsed.path.startswith("/xz-backdoor"):
            found.add("maintainer")
        if parsed.hostname in {"redhat.com", "www.redhat.com"} and \
                "urgent-security-alert" in parsed.path:
            found.add("vendor")
    return found


def main():
    try:
        data = json.loads((W / "dossier.json").read_text())
        loaded, error = isinstance(data, dict), ""
    except Exception as exc:
        data, loaded, error = {}, False, str(exc)
    checks = [check("valid dossier.json", loaded, error, 2)]
    incident = data.get("incident", {})
    checks.append(check("incident identity", isinstance(incident, dict)
                        and incident.get("cve") == "CVE-2024-3094"
                        and incident.get("disclosure_date") == "2024-03-29"
                        and incident.get("discoverer") == "Andres Freund"
                        and set(incident.get("affected_upstream_versions", []))
                        == {"5.6.0", "5.6.1"}, points=4))
    checks.append(check("construction chain", data.get("construction") == {
        "trigger_only_in_release_tarballs": True,
        "second_stage_artifacts_in_git": True,
    }, points=3))
    targeting = data.get("targeting", {})
    checks.append(check("build targeting", isinstance(targeting, dict)
                        and targeting.get("architecture") == "x86-64"
                        and targeting.get("os") == "Linux"
                        and targeting.get("libc") == "glibc"
                        and set(targeting.get("package_build_contexts", []))
                        == {"Debian", "RPM"}, points=4))
    checks.append(check("impact dependency path",
                        data.get("impact_path") == ["sshd", "libsystemd", "liblzma"],
                        points=2))
    red_hat = data.get("red_hat", {})
    checks.append(check("Red Hat impact and mitigation", isinstance(red_hat, dict)
                        and red_hat.get("rhel_affected") is False
                        and set(red_hat.get("affected_fedora_streams", []))
                        == {"Fedora Linux 40 beta", "Fedora Rawhide"}
                        and red_hat.get("recommended_safe_series") == "5.4.x",
                        points=3))
    sources = data.get("sources", [])
    checks.append(check("primary, maintainer, and vendor citations",
                        isinstance(sources, list)
                        and source_classes(sources)
                        == {"disclosure", "maintainer", "vendor"}, points=2))
    score = sum(item["points"] for item in checks if item["passed"])
    maximum = sum(item["points"] for item in checks)
    passed = all(item["passed"] for item in checks)
    print(json.dumps({"passed": passed, "score": score,
                      "max_score": maximum, "checks": checks}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
