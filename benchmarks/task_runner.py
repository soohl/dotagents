#!/usr/bin/env python3
"""Run outcome-graded task suites against a local model server."""

from __future__ import annotations

import argparse
import atexit
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

BENCHMARK_ROOT = Path(__file__).resolve().parent
SUITE_NAME = os.environ.get("PI_BENCH_SUITE", "agentic")
SUITES = {
    "agentic": {
        "title": "Agentic ability benchmark",
        "max_output": 4096,
        "tools": ("read", "write", "edit", "grep", "find", "ls", "bash"),
        "web_search": False,
    },
    "research": {
        "title": "Web research benchmark",
        "max_output": 4096,
        "tools": (
            "read",
            "write",
            "edit",
            "grep",
            "find",
            "ls",
            "bash",
            "web_search",
        ),
        "web_search": True,
    },
}
if SUITE_NAME not in SUITES:
    raise SystemExit(f"unknown benchmark suite: {SUITE_NAME}")
SUITE = SUITES[SUITE_NAME]
ROOT = BENCHMARK_ROOT / SUITE_NAME
TASK_ROOT = ROOT / "tasks"
RESULTS_ROOT = BENCHMARK_ROOT.parent / "results" / SUITE_NAME
RESULTS_PATH = RESULTS_ROOT / "results.jsonl"
TOOLS = SUITE["tools"]
WEB_SEARCH_ENABLED = SUITE["web_search"]
HOST_AGENT_DIR = Path(
    os.environ.get("PI_BENCH_HOST_AGENT_DIR", Path.home() / ".pi" / "agent")
).expanduser()
WEB_SEARCH_EXTENSION = Path(
    os.environ.get(
        "PI_BENCH_WEB_SEARCH_EXTENSION",
        HOST_AGENT_DIR / "npm" / "node_modules" / "pi-web-search",
    )
).expanduser()
WEB_SEARCH_PROVIDER = "openai-codex"
WEB_SEARCH_MODEL = "gpt-5.6-luna"
WEB_SEARCH_VERSION = "1.3.1"
WEB_SEARCH_ALLOWED_HOSTS = ("chatgpt.com:443", "auth.openai.com:443")
TASKS = {
    item["id"]: item
    for item in json.loads((ROOT / "tasks.json").read_text())["tasks"]
}
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
ACTIVE_RUN_DIRS: set[Path] = set()
ACTIVE_SANDBOXES: set[str] = set()


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    benchmark_model: str | None = None
    backend: str | None = None
    variant: str | None = None
    base_url: str | None = None
    reasoning: bool = False
    compat: dict | None = None
    thinking_level_map: dict | None = None
    compat_profile: str | None = None
    reasoning_profile: dict | None = None

    @property
    def name(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def report_name(self) -> str:
        return self.benchmark_model or self.name

    @classmethod
    def parse(cls, value: str) -> "ModelSpec":
        provider, separator, model = value.partition("/")
        if not separator or not provider.strip() or not model.strip():
            raise argparse.ArgumentTypeError(
                f"model must be PROVIDER/MODEL, not {value!r}"
            )
        return cls(provider.strip(), model.strip())


@dataclass(frozen=True)
class PiRuntime:
    version: str
    mount: Path
    entry: Path


def load(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


BASELINES = {}


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", value.lower()).strip("-")


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_capture(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, text=True, capture_output=True, **kwargs)


def discover_pi_runtime() -> PiRuntime:
    executable_name = os.environ.get("PI_BENCH_PI", "pi")
    executable = shutil.which(executable_name)
    if not executable:
        raise RuntimeError(f"Pi executable not found: {executable_name}")

    version_result = run_capture([executable, "--version"])
    if version_result.returncode:
        raise RuntimeError(version_result.stderr.strip() or "pi --version failed")
    version = version_result.stdout.strip()
    resolved = Path(executable).resolve()

    # Homebrew installs a small wrapper in Cellar/<version>/bin and the
    # relocatable Node application under the adjacent libexec directory.
    brew_libexec = resolved.parent.parent / "libexec"
    if (brew_libexec / "bin/pi").is_file():
        return PiRuntime(version, brew_libexec, brew_libexec / "bin/pi")

    # npm and development installs resolve the executable into the package.
    for parent in (resolved.parent, *resolved.parents):
        package_json = parent / "package.json"
        package = load(package_json, {})
        if package.get("name") == "@earendil-works/pi-coding-agent":
            entry = parent / "dist/cli.js"
            if entry.is_file():
                return PiRuntime(version, parent, entry)

    raise RuntimeError(
        "could not locate Pi's runtime; set PI_BENCH_PI to a supported Pi executable"
    )


def provider_auth(spec: ModelSpec) -> dict:
    if not spec.base_url:
        raise RuntimeError(f"local {SUITE_NAME} model URL is missing")
    auth = {spec.provider: {"type": "api_key", "key": "local"}}
    if not WEB_SEARCH_ENABLED:
        return auth
    host_auth = load(HOST_AGENT_DIR / "auth.json", {})
    search_credential = host_auth.get(WEB_SEARCH_PROVIDER)
    if not isinstance(search_credential, dict):
        raise RuntimeError(
            f"{WEB_SEARCH_PROVIDER} credential is missing from "
            f"{HOST_AGENT_DIR / 'auth.json'}"
        )
    auth[WEB_SEARCH_PROVIDER] = search_credential
    return auth


def rewrite_local_urls(value):
    if isinstance(value, dict):
        return {key: rewrite_local_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_local_urls(item) for item in value]
    if isinstance(value, str) and "://" in value:
        return re.sub(
            r"(?<=://)(?:127\.0\.0\.1|localhost|\[::1\])(?=[:/])",
            "host.docker.internal",
            value,
        )
    return value


def selected_models_config(
    spec: ModelSpec,
    context_window: int | None,
    max_output: int | None,
) -> dict:
    if not spec.base_url:
        raise RuntimeError(f"local {SUITE_NAME} model URL is missing")
    model = {
        "id": spec.model,
        "name": spec.benchmark_model or spec.model,
        "reasoning": spec.reasoning,
        "input": ["text"],
        "contextWindow": context_window or 65536,
        "maxTokens": max_output or SUITE["max_output"],
        "samplingParams": {"temperature": 0},
        "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
        },
    }
    if spec.compat:
        model["compat"] = dict(spec.compat)
    if spec.thinking_level_map:
        model["thinkingLevelMap"] = spec.thinking_level_map
    return {
        "providers": {
            spec.provider: {
                "baseUrl": rewrite_local_urls(spec.base_url),
                "api": "openai-completions",
                "apiKey": "local",
                "models": [model],
            }
        }
    }


def shared_settings(args: argparse.Namespace) -> dict:
    return {
        "defaultThinkingLevel": args.thinking,
        "enableAnalytics": False,
        "enableInstallTelemetry": False,
        "compaction": {
            "enabled": True,
            "reserveTokens": args.reserve_tokens,
            "keepRecentTokens": args.keep_recent_tokens,
        },
        "retry": {
            "enabled": True,
            "maxRetries": args.retries,
            "baseDelayMs": 1000,
        },
        "transport": args.transport,
        "packages": [],
        "extensions": [],
        "prompts": [],
        "themes": [],
    }


def prepare_run(
    task_id: str,
    spec: ModelSpec,
    args: argparse.Namespace,
) -> tuple[Path, Path, Path, str]:
    run_dir = Path(
        tempfile.mkdtemp(
            prefix=f"pi-{SUITE_NAME}-{task_id}-{slug(spec.report_name)}-"
        )
    )
    ACTIVE_RUN_DIRS.add(run_dir)

    workspace = run_dir / "workspace"
    shutil.copytree(TASK_ROOT / task_id / "workspace", workspace)
    prompt = (TASK_ROOT / task_id / "PROMPT.md").read_text()
    (workspace / "TASK.md").write_text(prompt)
    (run_dir / "sessions").mkdir()

    runtime_config = run_dir / ".pi-agent-runtime"
    runtime_config.mkdir()
    auth_path = runtime_config / "auth.json"
    auth_path.write_text(json.dumps(provider_auth(spec), indent=2) + "\n")
    auth_path.chmod(0o600)
    models = selected_models_config(spec, args.context_window, args.max_output)
    (runtime_config / "models.json").write_text(json.dumps(models, indent=2) + "\n")
    settings = shared_settings(args)
    (runtime_config / "settings.json").write_text(
        json.dumps(settings, indent=2) + "\n"
    )
    if WEB_SEARCH_ENABLED:
        (runtime_config / "web-search.json").write_text(
            json.dumps(
                {"provider": WEB_SEARCH_PROVIDER, "model": WEB_SEARCH_MODEL},
                indent=2,
            )
            + "\n"
        )

    return run_dir, workspace, runtime_config, prompt


def remove_sandbox(name: str | None) -> None:
    if name:
        subprocess.run(
            ["sbx", "rm", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        ACTIVE_SANDBOXES.discard(name)


def cleanup_active() -> None:
    for name in tuple(ACTIVE_SANDBOXES):
        remove_sandbox(name)
    for run_dir in tuple(ACTIVE_RUN_DIRS):
        shutil.rmtree(run_dir, ignore_errors=True)
        ACTIVE_RUN_DIRS.discard(run_dir)


def handle_termination(_signum, _frame) -> None:
    raise KeyboardInterrupt


atexit.register(cleanup_active)
signal.signal(signal.SIGTERM, handle_termination)


def create_sandbox(
    run_dir: Path,
    config: Path,
    runtime: PiRuntime,
    base_url: str,
) -> str:
    # Docker hostnames are limited to 63 characters. sbx derives the hostname
    # from this name, so keep it comfortably bounded.
    digest = hashlib.sha256(run_dir.name.encode()).hexdigest()[:8]
    name = f"pih-{slug(run_dir.name)[:44].rstrip('-')}-{digest}"
    mounts = [str(run_dir), f"{runtime.mount}:ro"]
    if WEB_SEARCH_ENABLED:
        mounts.append(f"{WEB_SEARCH_EXTENSION}:ro")
    create = run_capture(
        ["sbx", "create", "--quiet", "--name", name, "shell", *mounts]
    )
    if create.returncode:
        raise RuntimeError(create.stderr.strip())
    ACTIVE_SANDBOXES.add(name)
    try:
        parsed_url = urlparse(base_url)
        if not parsed_url.port:
            raise RuntimeError(f"local model URL has no explicit port: {base_url}")
        allowed_endpoints = ",".join(
            (
                *(
                    f"{host}:{parsed_url.port}"
                    for host in (
                        "host.docker.internal",
                        "localhost",
                        "127.0.0.1",
                    )
                ),
                *(WEB_SEARCH_ALLOWED_HOSTS if WEB_SEARCH_ENABLED else ()),
            )
        )
        policy = run_capture(
            [
                "sbx",
                "policy",
                "allow",
                "network",
                "--sandbox",
                name,
                allowed_endpoints,
            ]
        )
        if policy.returncode:
            raise RuntimeError(policy.stderr.strip())
        copied = run_capture(["sbx", "cp", str(config), f"{name}:/tmp/pi-agent"])
        if copied.returncode:
            raise RuntimeError(copied.stderr.strip())
        web_search_setup = ""
        if WEB_SEARCH_ENABLED:
            web_search_setup = (
                "cp /tmp/pi-agent/web-search.json "
                "/home/agent/.pi/agent/web-search.json && "
                "chown -R agent:agent /home/agent/.pi && "
                "chmod 600 /home/agent/.pi/agent/web-search.json"
            )
        secured = run_capture(
            [
                "sbx",
                "exec",
                "--user",
                "root",
                name,
                "sh",
                "-lc",
                "chown -R agent:agent /tmp/pi-agent && "
                "chmod 700 /tmp/pi-agent && "
                "chmod 600 /tmp/pi-agent/auth.json && "
                "mkdir -p /home/agent/.pi/agent"
                + (" && " + web_search_setup if web_search_setup else ""),
            ]
        )
        if secured.returncode:
            raise RuntimeError(secured.stderr.strip())
    except BaseException:
        remove_sandbox(name)
        raise
    finally:
        shutil.rmtree(config, ignore_errors=True)
    return name


def agent_command(
    sandbox: str,
    workspace: Path,
    run_dir: Path,
    runtime: PiRuntime,
    spec: ModelSpec,
    prompt: str,
    args: argparse.Namespace,
) -> list[str]:
    command = [
        "sbx",
        "exec",
        "--workdir",
        str(workspace),
        "--env",
        "PI_CODING_AGENT_DIR=/tmp/pi-agent",
        "--env",
        "PI_SKIP_VERSION_CHECK=1",
        "--env",
        "PI_TELEMETRY=0",
        "--env",
        f"PI_CACHE_RETENTION={args.cache_retention}",
        sandbox,
        "node",
        str(runtime.entry),
        "--mode",
        "json",
        "--provider",
        spec.provider,
        "--model",
        spec.name,
        "--thinking",
        args.thinking,
        "--session-dir",
        str(run_dir / "sessions"),
        "--tools",
        ",".join(TOOLS),
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
    ]
    if WEB_SEARCH_ENABLED:
        command[command.index("--no-extensions"):command.index("--no-extensions")] = [
            "--extension",
            str(WEB_SEARCH_EXTENSION),
        ]
    command.append(prompt)
    return command


def session_entries(run_dir: Path) -> list[dict]:
    entries = []
    for session in (run_dir / "sessions").glob("*.jsonl"):
        for line in session.read_text(errors="replace").splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def metrics(
    run_dir: Path,
    elapsed: float,
    returncode: int,
    requested_thinking: str,
) -> dict:
    events = []
    for line in (run_dir / "events.jsonl").read_text(errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    starts = [event for event in events if event.get("type") == "tool_execution_start"]
    ends = [event for event in events if event.get("type") == "tool_execution_end"]
    entries = session_entries(run_dir)
    usage = {key: 0 for key in ("input", "output", "cacheRead", "cacheWrite")}
    reasoning_chars = 0
    turns = 0
    for entry in entries:
        if entry.get("type") != "message":
            continue
        message = entry.get("message", {})
        if message.get("role") == "assistant":
            turns += 1
            for key in usage:
                usage[key] += int((message.get("usage") or {}).get(key, 0) or 0)
            reasoning_chars += sum(
                len(block.get("thinking", ""))
                for block in message.get("content", [])
                if block.get("type") == "thinking"
            )
    levels = [
        entry.get("thinkingLevel")
        for entry in entries
        if entry.get("type") == "thinking_level_change"
    ]
    result = {
        "elapsed_seconds": round(elapsed, 3),
        "pi_returncode": returncode,
        "model_turns": turns,
        "tool_call_count": len(starts),
        "tool_names": dict(Counter(event.get("toolName") for event in starts)),
        "tool_error_count": sum(bool(event.get("isError")) for event in ends),
        "compaction_count": sum(
            entry.get("type") == "compaction" for entry in entries
        ),
        "requested_thinking_level": requested_thinking,
        "effective_thinking_levels": levels,
        "generated_tokens": usage["output"],
        "reasoning_characters": reasoning_chars,
        "reasoning_tokens_estimated": (reasoning_chars + 3) // 4,
        "usage": {**usage, "total": sum(usage.values())},
    }
    return result


def grade_in_sandbox(task_id: str, run_dir: Path, sandbox: str) -> dict:
    grader = TASK_ROOT / task_id / "grader" / TASKS[task_id]["grader"]
    maximum = TASKS[task_id]["max_score"]
    destination = f"/tmp/{slug(task_id)}-grader.py"
    copied = run_capture(["sbx", "cp", str(grader), f"{sandbox}:{destination}"])
    if copied.returncode:
        score = {
            "passed": False,
            "score": 0,
            "max_score": maximum,
            "checks": [],
            "error": copied.stderr.strip(),
        }
    else:
        completed = run_capture(
            [
                "sbx",
                "exec",
                "--workdir",
                str(run_dir / "workspace"),
                sandbox,
                "python3",
                destination,
                str(run_dir / "workspace"),
            ],
            timeout=120,
        )
        try:
            score = json.loads(completed.stdout)
        except json.JSONDecodeError:
            score = {
                "passed": False,
                "score": 0,
                "max_score": maximum,
                "checks": [],
                "error": completed.stderr.strip() or completed.stdout,
            }
        score["grader_returncode"] = completed.returncode
        score["grader_stderr"] = completed.stderr
    score["grader_environment"] = "docker-sbx"
    return score


def execution_requirements(task_id: str, measured: dict) -> dict | None:
    required = TASKS[task_id].get("execution_requirements", {})
    if not required:
        return None
    web_calls = measured.get("tool_names", {}).get("web_search", 0)
    distinct_tools = len(
        {
            name
            for name, count in measured.get("tool_names", {}).items()
            if name and count
        }
    )
    checks = {
        "web_search_calls": {
            "actual": web_calls,
            "minimum": required.get("min_web_search_calls", 0),
        },
        "distinct_tools": {
            "actual": distinct_tools,
            "minimum": required.get("min_distinct_tools", 0),
        },
    }
    passed = all(item["actual"] >= item["minimum"] for item in checks.values())
    return {"passed": passed, "checks": checks}


def run_one(
    task_id: str,
    spec: ModelSpec,
    runtime: PiRuntime,
    args: argparse.Namespace,
) -> tuple[dict, bool]:
    run_dir, workspace, config, prompt = prepare_run(task_id, spec, args)
    sandbox = None
    returncode = 125
    elapsed = 0.0
    measured = {}
    score = {
        "passed": False,
        "score": 0,
        "max_score": TASKS[task_id]["max_score"],
        "error": "sandbox did not start",
    }
    configuration = {
        "reasoning_mode": spec.reasoning_profile,
        "compatibility_profile": spec.compat_profile,
        "pi_thinking_level": args.thinking,
        "temperature": 0,
        "context_window_override": args.context_window,
        "max_output_override": args.max_output,
        "reserve_tokens": args.reserve_tokens,
        "keep_recent_tokens": args.keep_recent_tokens,
        "tools": list(TOOLS),
        "agent_environment": "docker-sbx",
        "grader_environment": "docker-sbx",
        "credential_method": (
            "private sbx cp" if WEB_SEARCH_ENABLED else "local placeholder only"
        ),
        "network_policy": (
            "local model plus OpenAI web search"
            if WEB_SEARCH_ENABLED
            else "local model only"
        ),
    }
    if WEB_SEARCH_ENABLED:
        configuration["web_search"] = {
            "extension": "pi-web-search",
            "version": WEB_SEARCH_VERSION,
            "provider": WEB_SEARCH_PROVIDER,
            "model": WEB_SEARCH_MODEL,
        }
    metadata = {
        "timestamp": timestamp(),
        "task": task_id,
        "difficulty": TASKS[task_id]["difficulty"],
        "model": spec.report_name,
        "provider": spec.provider,
        "pi_model": spec.name,
        "backend": spec.backend,
        "variant": spec.variant,
        "pi_version": runtime.version,
        "configuration": configuration,
    }
    try:
        sandbox = create_sandbox(run_dir, config, runtime, spec.base_url)
        command = agent_command(
            sandbox, workspace, run_dir, runtime, spec, prompt, args
        )
        start = time.monotonic()
        with (run_dir / "events.jsonl").open("w") as stdout, (
            run_dir / "stderr.log"
        ).open("w") as stderr:
            try:
                completed = subprocess.run(
                    command,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    timeout=args.timeout,
                )
                returncode = completed.returncode
            except subprocess.TimeoutExpired:
                returncode = 124
        elapsed = time.monotonic() - start
        measured = metrics(run_dir, elapsed, returncode, args.thinking)
        score = grade_in_sandbox(task_id, run_dir, sandbox)
    except Exception as error:
        if not (run_dir / "events.jsonl").exists():
            (run_dir / "events.jsonl").write_text("")
        measured = metrics(run_dir, elapsed, returncode, args.thinking)
        score.update({"error": str(error), "grader_environment": "docker-sbx"})
    finally:
        remove_sandbox(sandbox)
        shutil.rmtree(config, ignore_errors=True)

    requirements = execution_requirements(task_id, measured)
    passed = (
        returncode == 0
        and bool(score.get("passed"))
        and (requirements is None or requirements["passed"])
    )
    if (
        requirements is not None
        and not requirements["passed"]
        and not score.get("error")
    ):
        score["error"] = "required research tool usage was not met"
    compact_score = {
        key: score[key]
        for key in (
            "passed",
            "score",
            "max_score",
            "checks",
            "error",
            "grader_returncode",
        )
        if key in score and score[key] not in (None, "", [])
    }
    result = {
        **metadata,
        "passed": passed,
        "metrics": measured,
        "score": compact_score,
    }
    if requirements is not None:
        result["execution_requirements"] = requirements
    stderr_text = ""
    stderr_path = run_dir / "stderr.log"
    if not passed and stderr_path.exists():
        stderr_text = stderr_path.read_text(errors="replace").strip()
    try:
        append_result(result)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
        ACTIVE_RUN_DIRS.discard(run_dir)

    print(
        f"{task_id} {spec.report_name}: {'PASS' if passed else 'FAIL'} "
        f"{score.get('score')}/{score.get('max_score')} in {elapsed:.1f}s; "
        f"{measured.get('generated_tokens', 0):,} generated, "
        f"≈{measured.get('reasoning_tokens_estimated', 0):,} reasoning"
    )
    if not passed:
        detail = score.get("error") or stderr_text
        if detail:
            print(detail[-4000:], file=sys.stderr)
    return result, passed


def append_result(result: dict) -> None:
    with RESULTS_PATH.open("a") as output:
        output.write(json.dumps(result, separators=(",", ":"), sort_keys=True))
        output.write("\n")


def result_entries() -> list[dict]:
    if not RESULTS_PATH.exists():
        return []
    entries = []
    for line in RESULTS_PATH.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def latest_results(
    model_filter: set[str] | None = None,
) -> dict[tuple[str, str], dict]:
    result = {}
    for entry in result_entries():
        model = entry.get("model")
        if model_filter and model not in model_filter:
            continue
        result[(entry.get("task"), model)] = entry
    return result


def write_report() -> None:
    latest = latest_results()
    model_names = sorted({model for _, model in latest})
    lines = [
        f"# {SUITE['title']}",
        "",
    ]
    if model_names:
        lines += [
            "| Task | Difficulty | Model | Result | Score | Time | Generated | Reasoning est. | Turns | Tools/errors | Cache R/W | Compactions |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    else:
        lines += ["No current local runs."]
    totals = {
        model: {
            "pass": 0,
            "runs": 0,
            "score": 0,
            "max": 0,
            "time": 0.0,
            "generated": 0,
            "reasoning": 0,
            "turns": 0,
            "tools": 0,
            "errors": 0,
        }
        for model in model_names
    }
    for task_id, task in TASKS.items():
        for model in model_names:
            record = latest.get((task_id, model))
            if record is None:
                lines.append(
                    f"| {task_id} | {task['difficulty']} | `{model}` | not run "
                    "| — | — | — | — | — | — | — | — | — |"
                )
                continue
            score = record.get("score", {})
            measured = record.get("metrics", {})
            passed = record.get("passed", False)
            usage = measured.get("usage", {})
            lines.append(
                f"| {task_id} | {task['difficulty']} | `{model}` "
                f"| {'PASS' if passed else 'FAIL'} "
                f"| {score.get('score', 0)}/{score.get('max_score', 0)} "
                f"| {measured.get('elapsed_seconds', 0):.1f}s "
                f"| {measured.get('generated_tokens', usage.get('output', 0)):,} "
                f"| ≈{measured.get('reasoning_tokens_estimated', 0):,} "
                f"| {measured.get('model_turns', 0)} "
                f"| {measured.get('tool_call_count', 0)}/{measured.get('tool_error_count', 0)} "
                f"| {usage.get('cacheRead', 0):,}/{usage.get('cacheWrite', 0):,} "
                f"| {measured.get('compaction_count', 0)} |"
            )
            total = totals[model]
            total["pass"] += bool(passed)
            total["runs"] += 1
            total["score"] += score.get("score", 0)
            total["max"] += score.get("max_score", 0)
            total["time"] += measured.get("elapsed_seconds", 0)
            total["generated"] += measured.get(
                "generated_tokens", usage.get("output", 0)
            )
            total["reasoning"] += measured.get(
                "reasoning_tokens_estimated", 0
            )
            total["turns"] += measured.get("model_turns", 0)
            total["tools"] += measured.get("tool_call_count", 0)
            total["errors"] += measured.get("tool_error_count", 0)
    if model_names:
        lines += [
            "",
            "## Totals",
            "",
            "| Model | Tasks passed | Task score | Time | Generated | Reasoning est. | Turns | Tools/errors |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for model, total in totals.items():
            lines.append(
                f"| `{model}` | {total['pass']}/{total['runs']} "
                f"| {total['score']}/{total['max']} | {total['time']:.1f}s "
                f"| {total['generated']:,} | ≈{total['reasoning']:,} "
                f"| {total['turns']} | {total['tools']}/{total['errors']} |"
            )
    if BASELINES:
        lines += [
            "",
            "## Historical baseline — not directly comparable",
            "",
            "These cloud results used an older, non-identical 65K profile. They "
            "are context only, not controlled comparisons with the local 64K runs.",
            "",
            "| Task | Model | Result | Score | Time | Turns | Tools/errors |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for model, baseline in BASELINES.items():
            for task_id, task in TASKS.items():
                result = baseline.get("tasks", {}).get(task_id)
                if not result:
                    continue
                lines.append(
                    f"| {task_id} | `{model}` "
                    f"| {'PASS' if result['passed'] else 'FAIL'} "
                    f"| {result['score']}/{result['max_score']} "
                    f"| {result['elapsed_seconds']:.1f}s "
                    f"| {result['model_turns']} "
                    f"| {result['tool_call_count']}/{result['tool_error_count']} |"
                )
    (RESULTS_ROOT / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(RESULTS_ROOT / "REPORT.md")


def sbx_status(run_diagnose: bool = False) -> tuple[bool, str]:
    if not shutil.which("sbx"):
        return False, "not installed"
    version = run_capture(["sbx", "version"])
    if version.returncode:
        return False, version.stderr.strip() or "sbx version failed"
    if run_diagnose:
        diagnose = run_capture(["sbx", "diagnose"], timeout=60)
        if diagnose.returncode:
            return False, diagnose.stderr.strip() or "sbx diagnose failed"
    return True, version.stdout.strip()


def self_test() -> int:
    checks: list[tuple[str, bool]] = []
    if WEB_SEARCH_ENABLED:
        web_package = load(WEB_SEARCH_EXTENSION / "package.json", {})
        checks.extend(
            [
                (
                    "pi-web-search extension",
                    web_package.get("name") == "pi-web-search"
                    and web_package.get("version") == WEB_SEARCH_VERSION,
                ),
                (
                    "OpenAI web-search credential",
                    isinstance(
                        load(HOST_AGENT_DIR / "auth.json", {}).get(
                            WEB_SEARCH_PROVIDER
                        ),
                        dict,
                    ),
                ),
            ]
        )
    else:
        checks.append(
            (
                "offline tool and network profile",
                "web_search" not in TOOLS and not WEB_SEARCH_ENABLED,
            )
        )
    for task_id, task in TASKS.items():
        prompt = TASK_ROOT / task_id / "PROMPT.md"
        workspace = TASK_ROOT / task_id / "workspace"
        grader = TASK_ROOT / task_id / "grader" / task["grader"]
        checks.extend(
            [
                (f"{task_id} prompt", prompt.is_file()),
                (f"{task_id} workspace", workspace.is_dir()),
                (f"{task_id} grader", grader.is_file()),
                (
                    f"{task_id} maximum",
                    isinstance(task.get("max_score"), int)
                    and task["max_score"] > 0,
                ),
            ]
        )

    sbx_ok, sbx_detail = sbx_status()
    checks.append((f"Docker sbx ({sbx_detail})", sbx_ok))
    sandbox = None
    if sbx_ok:
        digest = hashlib.sha256(str(ROOT).encode()).hexdigest()[:8]
        sandbox = f"pih-self-test-{digest}"
        remove_sandbox(sandbox)
        created = run_capture(
            [
                "sbx",
                "create",
                "--quiet",
                "--name",
                sandbox,
                "shell",
                str(ROOT),
            ]
        )
        checks.append(("Docker self-test sandbox", created.returncode == 0))
        if created.returncode == 0:
            try:
                for task_id, task in TASKS.items():
                    grader = TASK_ROOT / task_id / "grader" / task["grader"]
                    workspace = TASK_ROOT / task_id / "workspace"
                    destination = f"/tmp/{slug(task_id)}-grader.py"
                    copied = run_capture(
                        ["sbx", "cp", str(grader), f"{sandbox}:{destination}"]
                    )
                    completed = run_capture(
                        [
                            "sbx",
                            "exec",
                            "--workdir",
                            str(workspace),
                            sandbox,
                            "python3",
                            destination,
                            str(workspace),
                        ],
                        timeout=30,
                    ) if copied.returncode == 0 else copied
                    try:
                        output = json.loads(completed.stdout)
                        valid = (
                            isinstance(output.get("passed"), bool)
                            and output.get("max_score") == task["max_score"]
                        )
                    except json.JSONDecodeError:
                        valid = False
                    checks.append((f"{task_id} Docker grader", valid))
            finally:
                remove_sandbox(sandbox)

    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    passed = sum(ok for _, ok in checks)
    print(f"self-test: {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


def preflight(model: ModelSpec | None) -> int:
    checks: list[tuple[str, bool, str]] = []
    sbx_ok, sbx_detail = sbx_status(run_diagnose=True)
    checks.append(("Docker sbx", sbx_ok, sbx_detail))
    try:
        runtime = discover_pi_runtime()
        checks.append(("Pi runtime", True, f"{runtime.version} at {runtime.mount}"))
    except RuntimeError as error:
        checks.append(("Pi runtime", False, str(error)))
    if WEB_SEARCH_ENABLED:
        web_package = load(WEB_SEARCH_EXTENSION / "package.json", {})
        checks.append(
            (
                "OpenAI web search",
                web_package.get("name") == "pi-web-search"
                and web_package.get("version") == WEB_SEARCH_VERSION,
                (
                    f"pi-web-search {web_package.get('version')} at "
                    f"{WEB_SEARCH_EXTENSION}; "
                    f"{WEB_SEARCH_PROVIDER}/{WEB_SEARCH_MODEL}"
                ),
            )
        )

    for spec in [model] if model else []:
        try:
            with urllib.request.urlopen(
                spec.base_url.rstrip("/") + "/models", timeout=5
            ) as response:
                payload = json.load(response)
            ids = {
                item.get("id")
                for item in payload.get("data", [])
                if isinstance(item, dict)
            }
            ready = spec.model in ids
            detail = f"{spec.base_url} serves {sorted(ids)}"
        except (OSError, ValueError, urllib.error.URLError) as error:
            ready = False
            detail = (
                f"{error}; start it with: ./llm serve "
                f"{spec.benchmark_model} {spec.variant}"
            )
        checks.append((f"{spec.report_name} local endpoint", ready, detail))
        try:
            provider_auth(spec)
            selected_models_config(spec, None, None)
        except RuntimeError as error:
            checks.append(
                (f"{spec.name} sandbox configuration", False, str(error))
            )
        else:
            checks.append(
                (
                    f"{spec.name} sandbox configuration",
                    True,
                    "selected provider configuration is transferable",
                )
            )

    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    if not sbx_ok:
        print(
            "\nInstall Docker Sandboxes on macOS with:\n"
            "  brew install --cask docker/tap/sbx\n"
            "Then run: sbx diagnose",
            file=sys.stderr,
        )
    return 0 if all(ok for _, ok, _ in checks) else 1


def add_model_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "model",
        type=ModelSpec.parse,
        metavar="PROVIDER/MODEL",
        help="internal local provider/model ID supplied by ./llm",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Run the {SUITE_NAME} benchmark in Docker Sandboxes"
    )
    commands = parser.add_subparsers(dest="action", required=True)

    run_parser = commands.add_parser("run", help="run tasks for one model")
    add_model_argument(run_parser)
    run_parser.add_argument("--validate-only", action="store_true", help=argparse.SUPPRESS)
    run_parser.add_argument("--benchmark-model", help=argparse.SUPPRESS)
    run_parser.add_argument("--backend", help=argparse.SUPPRESS)
    run_parser.add_argument("--variant", help=argparse.SUPPRESS)
    run_parser.add_argument("--base-url", help=argparse.SUPPRESS)
    run_parser.add_argument("--reasoning", action="store_true", help=argparse.SUPPRESS)
    run_parser.add_argument("--compat-json", help=argparse.SUPPRESS)
    run_parser.add_argument("--thinking-level-map-json", help=argparse.SUPPRESS)
    run_parser.add_argument("--compat-profile", help=argparse.SUPPRESS)
    run_parser.add_argument("--reasoning-profile-json", help=argparse.SUPPRESS)
    run_parser.add_argument(
        "--task",
        action="append",
        choices=list(TASKS),
        help="run only this task (repeatable; default: all)",
    )
    run_parser.add_argument("--context-window", type=int, default=65536)
    run_parser.add_argument(
        "--max-output", type=int, help="default: up to 4096 tokens, within compaction reserve"
    )
    run_parser.add_argument("--reserve-tokens", type=int,
                            help="default: 10%% of context, rounded up (6554 at 64K)")
    run_parser.add_argument("--keep-recent-tokens", type=int,
                            help="default: up to 4096 tokens, within compaction reserve")
    run_parser.add_argument("--retries", type=int, default=2)
    run_parser.add_argument(
        "--transport", choices=("sse", "websocket", "auto"), default="sse"
    )
    run_parser.add_argument(
        "--cache-retention", choices=("short", "long"), default="long"
    )
    run_parser.add_argument(
        "--timeout", type=int, default=7200, help="seconds per model/task"
    )

    commands.add_parser(
        "report", help="report the latest run for each model/task"
    )
    commands.add_parser("self-test", help="validate all tasks and graders in sbx")
    return parser


def validate_run_args(args: argparse.Namespace) -> None:
    if not 2 <= args.context_window <= 65536:
        raise SystemExit("--context-window must be between 2 and 65536 tokens")
    if args.reserve_tokens is None:
        args.reserve_tokens = (args.context_window + 9) // 10
    if not 1024 < args.reserve_tokens < args.context_window:
        raise SystemExit("--reserve-tokens must exceed 1024 and be smaller than the context")
    if args.max_output is None:
        args.max_output = min(SUITE["max_output"], args.reserve_tokens - 1024)
    if args.keep_recent_tokens is None:
        args.keep_recent_tokens = min(4096, args.reserve_tokens - 1024)
    positive = (
        "context_window",
        "max_output",
        "reserve_tokens",
        "keep_recent_tokens",
        "timeout",
    )
    for name in positive:
        value = getattr(args, name)
        if value is not None and value <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if args.retries < 0:
        raise SystemExit("--retries cannot be negative")
    if args.keep_recent_tokens >= args.reserve_tokens:
        raise SystemExit("--keep-recent-tokens must be less than --reserve-tokens")
    if args.max_output > args.reserve_tokens - 1024:
        raise SystemExit("--max-output must leave at least 1024 tokens within --reserve-tokens; "
                         "reduce output or compact earlier with a larger reserve")


def main(argv: list[str] | None = None) -> int:
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(arguments)

    if args.action == "self-test":
        return self_test()
    if args.action == "report":
        write_report()
        return 0

    validate_run_args(args)
    if args.validate_only:
        return 0
    if not args.base_url:
        raise SystemExit(
            f"{SUITE_NAME} benchmarks are local-only; run them through "
            f"./llm benchmark {SUITE_NAME} <model> [variant]"
        )
    if args.base_url:
        if not args.compat_profile or not args.reasoning_profile_json:
            raise SystemExit(
                f"local {SUITE_NAME} runs require compatibility and "
                "reasoning profiles"
            )
        try:
            compat = json.loads(args.compat_json) if args.compat_json else None
            thinking_map = (
                json.loads(args.thinking_level_map_json)
                if args.thinking_level_map_json
                else None
            )
            reasoning_profile = json.loads(args.reasoning_profile_json)
        except json.JSONDecodeError as error:
            raise SystemExit(f"invalid local model compatibility JSON: {error}")
        if (
            not isinstance(reasoning_profile, dict)
            or reasoning_profile.get("mode") != "native-strong"
            or reasoning_profile.get("piThinking") not in THINKING_LEVELS
        ):
            raise SystemExit("invalid native-strong reasoning profile")
        args.thinking = reasoning_profile["piThinking"]
        args.model = replace(
            args.model,
            benchmark_model=args.benchmark_model,
            backend=args.backend,
            variant=args.variant,
            base_url=args.base_url,
            reasoning=args.reasoning,
            compat=compat,
            thinking_level_map=thinking_map,
            compat_profile=args.compat_profile,
            reasoning_profile=reasoning_profile,
        )
    if preflight(args.model):
        return 1
    runtime = discover_pi_runtime()
    task_ids = args.task or list(TASKS)
    rows = []
    for task_id in task_ids:
        rows.append(run_one(task_id, args.model, runtime, args))
    write_report()
    return 0 if all(passed for _, passed in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
