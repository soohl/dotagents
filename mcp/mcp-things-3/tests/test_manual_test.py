from unittest.mock import AsyncMock

import pytest

from things_mcp.manual_test import MARKER, TASK_TITLE, find_fixture, run


async def test_manual_check_rejects_other_projects_before_reading():
    backend = AsyncMock()
    with pytest.raises(ValueError, match="only accepts"):
        await find_fixture(backend, "My real project")
    backend.query.assert_not_awaited()


@pytest.mark.parametrize("notes", ["original notes", MARKER])
async def test_manual_check_scopes_task_to_project_and_blocks_repeat(notes):
    backend = AsyncMock()
    title = "MCP encoding retest fixture"
    backend.query.side_effect = [
        {"items": [{"id": "project1", "title": title}]},
        {"items": [{"id": "task1", "title": TASK_TITLE, "notes": notes}]},
    ]
    if notes == MARKER:
        with pytest.raises(ValueError, match="already contains"):
            await find_fixture(backend, title)
    else:
        assert await find_fixture(backend, title) == ("project1", "task1")
    assert backend.query.call_args.args[0].container.id == "project1"


async def test_manual_check_respects_read_only(monkeypatch):
    monkeypatch.setenv("THINGS_READ_ONLY", "true")
    with pytest.raises(ValueError, match="THINGS_READ_ONLY"):
        await run("MCP encoding retest fixture")
