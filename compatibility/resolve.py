#!/usr/bin/env python3

import argparse
import json
import re
import shlex
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def load_profile(name: str, stack: tuple[str, ...] = ()) -> dict:
    if not PROFILE_NAME.fullmatch(name):
        raise ValueError(f"invalid compatibility profile name: {name}")
    if name in stack:
        raise ValueError(
            "compatibility profile cycle: " + " -> ".join((*stack, name))
        )
    path = ROOT / f"{name}.json"
    try:
        profile = json.loads(path.read_text())
    except FileNotFoundError:
        raise ValueError(f"compatibility profile not found: {path}") from None
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid compatibility profile {path}: {error}") from error
    if not isinstance(profile, dict):
        raise ValueError(f"compatibility profile must be an object: {path}")
    parent = profile.pop("extends", None)
    if parent is None:
        return profile
    if not isinstance(parent, str):
        raise ValueError(f"'extends' must be a profile name: {path}")
    return merge(load_profile(parent, (*stack, name)), profile)


def validate(profile: dict, expected_backend: str) -> None:
    unknown = set(profile) - {"backend", "pi", "benchmark"}
    if unknown:
        raise ValueError(f"unknown profile fields: {', '.join(sorted(unknown))}")
    backend = profile.get("backend")
    if backend != expected_backend:
        raise ValueError(
            f"profile backend is {backend!r}, expected {expected_backend!r}"
        )
    pi = profile.get("pi", {})
    if not isinstance(pi, dict):
        raise ValueError("'pi' must be an object")
    if set(pi) - {"compat", "thinkingLevelMap"}:
        raise ValueError("unknown fields in profile 'pi' section")
    if "compat" in pi and not isinstance(pi["compat"], dict):
        raise ValueError("'pi.compat' must be an object")
    if "thinkingLevelMap" in pi and not isinstance(
        pi["thinkingLevelMap"], dict
    ):
        raise ValueError("'pi.thinkingLevelMap' must be an object")
    benchmark = profile.get("benchmark", {})
    if not isinstance(benchmark, dict):
        raise ValueError("'benchmark' must be an object")
    if set(benchmark) - {"reasoning"}:
        raise ValueError("unknown fields in profile 'benchmark' section")
    reasoning = benchmark.get("reasoning")
    if reasoning is not None:
        if not isinstance(reasoning, dict):
            raise ValueError("'benchmark.reasoning' must be an object")
        required = {"mode", "piThinking", "control", "nativeValue"}
        if set(reasoning) != required:
            raise ValueError(
                "'benchmark.reasoning' requires exactly: "
                + ", ".join(sorted(required))
            )
        if reasoning["mode"] != "native-strong":
            raise ValueError(
                "'benchmark.reasoning.mode' must be 'native-strong'"
            )
        if reasoning["piThinking"] not in {
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("invalid 'benchmark.reasoning.piThinking'")
        if not isinstance(reasoning["control"], str) or not isinstance(
            reasoning["nativeValue"], str
        ):
            raise ValueError(
                "benchmark reasoning control and nativeValue must be strings"
            )


def shell_values(profile: dict) -> dict[str, str]:
    pi = profile.get("pi", {})
    values = {
        "PI_COMPAT_JSON": json.dumps(
            pi.get("compat", {}), separators=(",", ":"), sort_keys=True
        ),
        "PI_THINKING_LEVEL_MAP_JSON": json.dumps(
            pi.get("thinkingLevelMap", {}), separators=(",", ":"), sort_keys=True
        ),
        "BENCHMARK_REASONING_JSON": json.dumps(
            profile.get("benchmark", {}).get("reasoning", {}),
            separators=(",", ":"),
            sort_keys=True,
        ),
    }
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="resolve a local API profile")
    parser.add_argument("profile")
    parser.add_argument("--backend", required=True)
    parser.add_argument("--format", choices=("json", "shell"), default="json")
    args = parser.parse_args()
    try:
        profile = load_profile(args.profile)
        validate(profile, args.backend)
    except ValueError as error:
        parser.error(str(error))
    if args.format == "json":
        print(json.dumps(profile, indent=2, sort_keys=True))
    else:
        for name, value in shell_values(profile).items():
            print(f"export {name}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
