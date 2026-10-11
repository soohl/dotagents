"""Opt-in local test. Creates owned fixtures and leaves one project in Things Trash."""

import os
import uuid
from datetime import date

import pytest

from things_mcp.backend import AppleScript, AutomationError
from things_mcp.models import Fields, Query, Target
from things_mcp.server import create_server

pytestmark = pytest.mark.skipif(
    os.environ.get("THINGS_MCP_LIVE_TEST") != "1",
    reason="set THINGS_MCP_LIVE_TEST=1 for fixture writes",
)


async def test_live_lifecycle():
    backend = AppleScript()
    suffix = uuid.uuid4().hex[:10]
    title = f"MCP verification {suffix}"
    owned = []
    try:
        tag = await backend.create("tag", Fields(title=title))
        owned.append(("tag", tag["id"]))
        child_tag = await backend.create(
            "tag", Fields(title=title + " child", parent_tag_id=tag["id"])
        )
        owned.append(("tag", child_tag["id"]))
        assert child_tag["parent_tag_id"] == tag["id"]
        with pytest.raises(AutomationError):
            await backend.update("tag", tag["id"], Fields(parent_tag_id=child_tag["id"]))
        detached_tag = await backend.update("tag", child_tag["id"], Fields(parent_tag_id=""))
        assert detached_tag.get("parent_tag_id") is None
        area = await backend.create("area", Fields(title=title))
        owned.append(("area", area["id"]))
        project = await backend.create(
            "project", Fields(title=title), Target(kind="area", id=area["id"])
        )
        owned.append(("project", project["id"]))
        task = await backend.create(
            "to-do",
            Fields(
                title=title,
                notes='Unicode 雪\n"quotes" \\ backslash',
                tags=[title],
                deadline=date(2027, 1, 2),
            ),
            Target(kind="project", id=project["id"]),
        )
        owned.append(("to-do", task["id"]))
        assert task["notes"] == 'Unicode 雪\n"quotes" \\ backslash'
        assert task["tags"] == [title]
        assert task["project_id"] == project["id"]
        assert task["deadline"].startswith("2027-01-02")
        for query in (
            Query(text=suffix),
            Query(tag=title),
            Query(status="open", text=suffix),
            Query(container=Target(kind="project", id=project["id"])),
        ):
            found = await backend.query(query)
            assert task["id"] in [item["id"] for item in found["items"]]
        updated = await backend.update("to-do", task["id"], Fields(status="completed"))
        assert updated["status"] == "completed"
        updated = await backend.update(
            "to-do", task["id"], Fields(status="open", notes="", tags=[], deadline="")
        )
        assert updated["status"] == "open"
        assert updated["notes"] == ""
        assert updated["tags"] == []
        assert updated.get("deadline") is None
        server = create_server(backend, read_only=False)
        await server.call_tool(
            "things_schedule",
            {
                "kind": "to-do",
                "item_id": task["id"],
                "start_date": "2027-02-03",
            },
        )
        scheduled = await backend.get("to-do", task["id"])
        assert scheduled["activation_date"].startswith("2027-02-03")
        detached = await backend.move("to-do", task["id"], None)
        assert detached.get("project_id") is None
        assert detached.get("area_id") is None
        # Keep the fixture in its project so cleanup trashes it with that project.
        moved = await backend.move("to-do", task["id"], Target(kind="project", id=project["id"]))
        assert moved["project_id"] == project["id"]
        lists = await backend.query(Query(kind="list"))
        assert lists["total"] >= 7
        today = next(item for item in lists["items"] if item["id"] == "TMTodayListSource")
        today_task = await backend.create(
            "to-do", Fields(title=title + " today"), Target(kind="list", id=today["id"])
        )
        owned.append(("to-do", today_task["id"]))
        today_items = await backend.query(Query(container=Target(kind="list", id=today["id"])))
        assert today_task["id"] in [item["id"] for item in today_items["items"]]
        await backend.move("to-do", today_task["id"], Target(kind="project", id=project["id"]))
        print("Live lifecycle verified. One verification project was moved to Trash.")
    finally:
        for kind, item_id in reversed(owned):
            await backend.delete(kind, item_id)
