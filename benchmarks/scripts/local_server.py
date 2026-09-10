#!/usr/bin/env python3

"""Manage local benchmark server startup checks."""

import argparse
import json
import os
import socket
import time
import urllib.error
import urllib.request


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        print(sock.getsockname()[1])


def wait_ready(args):
    url = args.base_url.rstrip("/") + "/models"
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            os.kill(args.pid, 0)
        except OSError:
            raise SystemExit("local model server exited during startup")
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.load(response)
            if args.model in {
                item.get("id")
                for item in payload.get("data", [])
                if isinstance(item, dict)
            }:
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    raise SystemExit("timed out waiting for the local model server")


def parse_args():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("free-port")

    wait_parser = subparsers.add_parser("wait-ready")
    wait_parser.add_argument("--pid", type=int, required=True)
    wait_parser.add_argument("--base-url", required=True)
    wait_parser.add_argument("--model", required=True)
    wait_parser.add_argument("--timeout", type=float, default=600)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "free-port":
        free_port()
    else:
        wait_ready(args)


if __name__ == "__main__":
    main()
