#!/usr/bin/env python3

"""Compare target-only and DSpark decoding through the native DS4 CLI."""

import argparse
import json
import os
import re
import subprocess


TIMING_RE = re.compile(
    r"prefill:\s+([0-9.]+) t/s, generation:\s+([0-9.]+) t/s"
)
STATS_RE = re.compile(r"^ds4: DSpark stats (.+)$", re.MULTILINE)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--support", required=True)
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--generated", type=int, required=True)
    parser.add_argument("--context", type=int, required=True)
    parser.add_argument("--prefill-chunk", type=int, required=True)
    parser.add_argument("--confidence", type=float, required=True)
    return parser.parse_args()


def parse_stats(stderr):
    match = STATS_RE.search(stderr)
    if not match:
        raise SystemExit("DS4 did not report DSpark statistics")
    values = {}
    for key, value in re.findall(r"([a-z_]+)=([0-9.]+)%?", match.group(1)):
        values[key] = float(value)
    for required in ("proposed", "accepted_draft", "errors"):
        if required not in values:
            raise SystemExit(f"DS4 statistics are missing {required}")
    if values["errors"]:
        raise SystemExit(f"DSpark verifier reported errors: {match.group(1)}")
    return values


def run(args, prompt, speculative):
    command = [
        args.binary,
        "--metal",
        "--model",
        args.model,
        "--ctx",
        str(args.context),
        "--prefill-chunk",
        str(args.prefill_chunk),
        "--tokens",
        str(args.generated),
        "--temp",
        "0",
        "--nothink",
        "-p",
        prompt,
    ]
    environment = os.environ.copy()
    if speculative:
        command.extend(
            [
                "--mtp-model",
                args.support,
                "--dspark",
                "--dspark-confidence",
                str(args.confidence),
            ]
        )
        environment["DS4_DSPARK_STATS"] = "1"

    result = subprocess.run(
        command,
        cwd=os.path.dirname(os.path.abspath(args.binary)),
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        raise SystemExit(
            f"{'DSpark' if speculative else 'target'} run failed:\n"
            f"{result.stderr}"
        )
    timing = TIMING_RE.search(result.stderr)
    if not timing:
        raise SystemExit("DS4 did not report benchmark timing")
    row = {
        "prefill": float(timing.group(1)),
        "decode": float(timing.group(2)),
    }
    if speculative:
        row["stats"] = parse_stats(result.stderr)
    return row


def main():
    args = parse_args()
    if args.generated <= 0 or not args.generated < args.context <= 65536:
        raise SystemExit("invalid generation or context size")
    if not 0 <= args.confidence <= 1:
        raise SystemExit("DSpark confidence must be between 0 and 1")
    with open(args.prompts, encoding="utf-8") as prompt_file:
        prompts = json.load(prompt_file)
    if not prompts:
        raise SystemExit("speculative prompt suite is empty")

    rows = []
    for item in prompts:
        target = run(args, item["prompt"], speculative=False)
        speculative = run(args, item["prompt"], speculative=True)
        proposed = speculative["stats"]["proposed"]
        accepted = speculative["stats"]["accepted_draft"]
        rows.append(
            {
                "name": item["name"],
                "target": target["decode"],
                "speculative": speculative["decode"],
                "proposed": proposed,
                "accepted": accepted,
            }
        )

    print(
        "| Prompt | Token limit | Target decode | DSpark decode "
        "| Speedup | Acceptance |"
    )
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    for row in rows:
        speedup = row["speculative"] / row["target"]
        acceptance = row["accepted"] / row["proposed"] if row["proposed"] else 0
        print(
            f"| {row['name'].title()} | {args.generated:,} "
            f"| {row['target']:.2f} t/s | {row['speculative']:.2f} t/s "
            f"| {speedup:.2f}x | {acceptance:.1%} |"
        )

    target_average = sum(row["target"] for row in rows) / len(rows)
    speculative_average = (
        sum(row["speculative"] for row in rows) / len(rows)
    )
    proposed = sum(row["proposed"] for row in rows)
    accepted = sum(row["accepted"] for row in rows)
    print(
        f"| **Macro average** | — | **{target_average:.2f} t/s** "
        f"| **{speculative_average:.2f} t/s** "
        f"| **{speculative_average / target_average:.2f}x** "
        f"| **{accepted / proposed:.1%}** |"
    )


if __name__ == "__main__":
    main()
