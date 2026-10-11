"""Interactive URL update check, restricted to the disposable visual-test project."""

import argparse
import asyncio
import getpass
import sys
import warnings

from .backend import AppleScript, AutomationError
from .models import Query, Target, URLFields
from .server import read_only_setting
from .urls import build_url

MARKER = "URL update test: café + tea"
TASK_TITLE = "Pack test bag + spare"


async def find_fixture(backend: AppleScript, project_title: str) -> tuple[str, str]:
    if not project_title.startswith("MCP encoding retest "):
        raise ValueError("This check only accepts an MCP encoding retest project.")
    result = await backend.query(Query(kind="project", text=project_title, limit=200))
    matches = [item for item in result["items"] if item["title"] == project_title]
    if len(matches) != 1 or result.get("next_offset") is not None:
        raise ValueError("Expected exactly one matching test project. No update was sent.")
    project_id = matches[0]["id"]
    result = await backend.query(Query(container=Target(kind="project", id=project_id), limit=200))
    matches = [item for item in result["items"] if item["title"] == TASK_TITLE]
    if len(matches) != 1 or result.get("next_offset") is not None:
        raise ValueError("Expected exactly one matching test task. No update was sent.")
    if MARKER in matches[0].get("notes", ""):
        raise ValueError("This task already contains the update marker. No update was sent.")
    return project_id, matches[0]["id"]


async def run(project_title: str):
    if read_only_setting():
        raise ValueError("THINGS_READ_ONLY is enabled. No update was sent.")
    backend = AppleScript()
    project_id, task_id = await find_fixture(backend, project_title)
    print(f"Test project: {project_title}\nTest task: {TASK_TITLE}")
    with warnings.catch_warnings():
        # Fail instead of falling back to a prompt that could display the token.
        warnings.simplefilter("error", getpass.GetPassWarning)
        token = getpass.getpass("Things URL token (hidden; not saved): ").strip()
    if not token:
        raise ValueError("No token entered. No update was sent.")
    fields = URLFields(
        append_notes="\n" + MARKER,
        append_checklist_items=["Tickets + receipt"],
        when="tomorrow@10:30",
        list_id=project_id,
        heading="Follow-up",
    )
    await backend.dispatch_url(build_url("update", fields, task_id, token))
    print("Update dispatched once. Check Things before any retry.")
    print("Expected: task under Follow-up; reminder tomorrow at 10:30 AM;")
    print("Tickets + receipt added unchecked; notes end with: " + MARKER)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_title", help="exact title of the disposable test project")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.project_title))
    except (AutomationError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        print(
            "A hidden token prompt requires an interactive terminal. No update was sent.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
