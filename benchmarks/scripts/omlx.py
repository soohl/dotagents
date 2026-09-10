#!/usr/bin/env python3

"""Run the shared single-stream speed sweep through oMLX."""

import argparse
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--model-root", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--settings-dir", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--ctx-start", type=int, required=True)
    parser.add_argument("--ctx-max", type=int, required=True)
    parser.add_argument("--step", type=int, required=True)
    parser.add_argument("--generated", type=int, required=True)
    parser.add_argument(
        "--speculative", choices=("off", "on", "compare"), default="off"
    )
    return parser.parse_args()


def context_sweep(start, maximum, step):
    if start <= 0 or maximum < start or step <= 1:
        raise SystemExit("invalid benchmark context or step setting")
    contexts = []
    value = start
    while value <= maximum:
        contexts.append(value)
        value *= step
    if contexts[-1] != maximum:
        contexts.append(maximum)
    return contexts


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def update_speculative(settings_dir, enabled):
    path = os.path.join(settings_dir, "model_settings.json")
    with open(path, encoding="utf-8") as handle:
        settings = json.load(handle)
    for model in settings["models"].values():
        model["mtp_enabled"] = enabled
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def rate(usage, direct_key, tokens_key, duration_key):
    direct = usage.get(direct_key)
    if direct is not None:
        return float(direct)
    duration = usage.get(duration_key)
    if duration:
        return float(usage[tokens_key]) / float(duration)
    raise SystemExit(f"oMLX usage is missing {direct_key} and {duration_key}")


def run_profile(args, contexts, speculative):
    with open(os.path.join(args.settings_dir, 'model_settings.json'), encoding='utf-8') as handle:
        models = json.load(handle)['models']
    for settings in models.values():
        capacity = settings['max_context_window']
        if not 2 <= capacity <= 65536 or not 0 < args.generated <= capacity - max(contexts):
            raise SystemExit('Speed sweep must fit prompt and output within a context of at most 65536')
    update_speculative(args.settings_dir, speculative)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["OMLX_BASE_PATH"] = args.settings_dir
    process = subprocess.Popen(
        [
            args.server,
            "serve",
            "--model-dir",
            args.model_root,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--max-concurrent-requests",
            "1",
            "--no-cache",
            "--no-hf-cache",
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def request(path, payload=None, timeout=1200):
        data = None if payload is None else json.dumps(payload).encode()
        request_object = urllib.request.Request(
            base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request_object, timeout=timeout) as response:
            return json.load(response)

    def completion(prompt, generated):
        payload = {
            "model": args.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": generated,
            "temperature": 0,
            "top_p": 1,
            "seed": 42,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        request_object = urllib.request.Request(
            base_url + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        usage = None
        with urllib.request.urlopen(request_object, timeout=1200) as response:
            for raw_line in response:
                line = raw_line.decode().strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                event = json.loads(data)
                if event.get("usage"):
                    usage = event["usage"]
        if usage is None:
            raise SystemExit("oMLX stream completed without usage statistics")
        return {"usage": usage}

    def benchmark_prompt(source):
        if os.environ.get("OMLX_BENCH_FORCE_GENERATION", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            return (
                source
                + "\n\nThe source text ends here. Output the integers from 1 "
                "through 1000 in order, one integer per line. Do not explain, "
                "summarize, or stop before 1000."
            )
        return "Continue the following text without summarizing it:\n\n" + source

    try:
        deadline = time.monotonic() + 600
        while True:
            if process.poll() is not None:
                raise SystemExit("oMLX exited while loading the model catalog")
            try:
                payload = request("/v1/models", timeout=5)
                if args.model in {
                    item.get("id") for item in payload.get("data", [])
                }:
                    break
            except (OSError, ValueError, urllib.error.URLError):
                pass
            if time.monotonic() >= deadline:
                raise SystemExit("timed out waiting for oMLX")
            time.sleep(0.5)

        completion("Warm up. Reply with eight words.", 8)
        with open(args.prompt, encoding="utf-8") as prompt_file:
            shared_prompt = prompt_file.read()

        calibration_chars = min(len(shared_prompt), args.ctx_start * 4)
        calibration = completion(shared_prompt[:calibration_chars], 1)
        calibration_tokens = calibration["usage"]["prompt_tokens"]
        chars_per_token = calibration_chars / calibration_tokens

        rows = []
        for wanted_context in contexts:
            prefix_chars = min(
                len(shared_prompt),
                max(1, round(wanted_context * chars_per_token)),
            )
            result = completion(
                benchmark_prompt(shared_prompt[:prefix_chars]),
                args.generated,
            )
            usage = result["usage"]
            if int(usage["completion_tokens"]) != args.generated:
                raise SystemExit(
                    "oMLX generated "
                    f"{usage['completion_tokens']} of {args.generated} requested "
                    "tokens; use a benchmark prompt that does not terminate early"
                )
            rows.append(
                {
                    "wanted_context": wanted_context,
                    "context": int(usage["prompt_tokens"]),
                    "prefill": rate(
                        usage,
                        "prompt_tokens_per_second",
                        "prompt_tokens",
                        "prompt_eval_duration",
                    ),
                    "generated": int(usage["completion_tokens"]),
                    "decode": rate(
                        usage,
                        "generation_tokens_per_second",
                        "completion_tokens",
                        "generation_duration",
                    ),
                }
            )
        return rows
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def print_single(rows):
    print("| Target context | Actual prompt | Prefill | Generated | Decode |")
    print("| ---: | ---: | ---: | ---: | ---: |")
    for row in rows:
        print(
            f"| {row['wanted_context']:,} | {row['context']:,} "
            f"| {row['prefill']:.2f} t/s | {row['generated']:,} "
            f"| {row['decode']:.2f} t/s |"
        )


def print_comparison(target_rows, speculative_rows):
    print(
        "| Target context | Target prompt | MTP prompt | Generated "
        "| Target prefill | MTP prefill | Prefill ratio "
        "| Target decode | MTP decode | Decode ratio |"
    )
    print(
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: "
        "| ---: | ---: | ---: |"
    )
    for target, speculative in zip(
        target_rows, speculative_rows, strict=True
    ):
        prefill_ratio = speculative["prefill"] / target["prefill"]
        decode_ratio = speculative["decode"] / target["decode"]
        print(
            f"| {target['wanted_context']:,} | {target['context']:,} "
            f"| {speculative['context']:,} | {target['generated']:,} "
            f"| {target['prefill']:.2f} t/s "
            f"| {speculative['prefill']:.2f} t/s "
            f"| {prefill_ratio:.2f}x "
            f"| {target['decode']:.2f} t/s "
            f"| {speculative['decode']:.2f} t/s "
            f"| {decode_ratio:.2f}x |"
        )


def main():
    args = parse_args()
    if args.generated <= 0:
        raise SystemExit("generated token count must be positive")
    contexts = context_sweep(args.ctx_start, args.ctx_max, args.step)
    if args.speculative == "compare":
        target = run_profile(args, contexts, speculative=False)
        speculative = run_profile(args, contexts, speculative=True)
        print_comparison(target, speculative)
    else:
        rows = run_profile(
            args, contexts, speculative=args.speculative == "on"
        )
        print_single(rows)


if __name__ == "__main__":
    main()
