import json
import sys

from .core import summarize


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m ledgerlite INPUT.json")
        return 0
    with open(args[0], encoding="utf-8") as handle:
        print(json.dumps(summarize(json.load(handle))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
