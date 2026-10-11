import os
import sys
from unittest.mock import AsyncMock

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from things_mcp.server import create_server


@pytest.fixture
def backend():
    result = AsyncMock()
    result.timeout = 45
    result.health.return_value = {"version": "test"}
    result.create.return_value = {"id": "new", "title": "Test"}
    return result


async def test_tool_schema_and_annotations(backend, monkeypatch):
    monkeypatch.delenv("THINGS_SHORTCUTS_CONFIG", raising=False)
    server = create_server(backend)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert len(tools) == 15
    assert tools["things_query"].annotations.readOnlyHint
    assert tools["things_delete"].annotations.destructiveHint
    assert not tools["things_url_create"].annotations.readOnlyHint
    assert tools["things_run_shortcut"].annotations.openWorldHint
    assert "fields" in tools["things_create"].inputSchema["properties"]


@pytest.mark.parametrize(
    "name,args",
    [
        ("things_create", {"kind": "to-do", "fields": {"title": "blocked"}}),
        ("things_update", {"kind": "to-do", "item_id": "x", "fields": {"title": "blocked"}}),
        ("things_move", {"kind": "to-do", "item_id": "x"}),
        ("things_schedule", {"kind": "to-do", "item_id": "x", "start_date": "2027-01-01"}),
        ("things_delete", {"kind": "to-do", "item_id": "x"}),
        ("things_url_update", {"kind": "to-do", "item_id": "x", "fields": {"when": "evening"}}),
        ("things_url_create", {"kind": "to-do", "fields": {"title": "blocked"}}),
        ("things_create_project_template", {"template": {"title": "blocked"}}),
        ("things_show", {"item_id": "today"}),
        ("things_search_ui", {"query": "x"}),
        ("things_run_shortcut", {"alias": "x", "payload": {}}),
    ],
)
async def test_read_only_blocks_every_mutation(backend, name, args, monkeypatch):
    monkeypatch.delenv("THINGS_SHORTCUTS_CONFIG", raising=False)
    server = create_server(backend, read_only=True)
    with pytest.raises(Exception, match="THINGS_READ_ONLY"):
        await server.call_tool(name, args)
    assert not backend.mock_calls


async def test_create_typed_arguments(backend, monkeypatch):
    monkeypatch.delenv("THINGS_SHORTCUTS_CONFIG", raising=False)
    server = create_server(backend, read_only=False)
    await server.call_tool("things_create", {"kind": "to-do", "fields": {"title": "Test"}})
    assert backend.create.call_args.args[1].title == "Test"


async def test_unconfigured_shortcut_rejected(backend, monkeypatch):
    monkeypatch.delenv("THINGS_SHORTCUTS_CONFIG", raising=False)
    server = create_server(backend, read_only=False)
    with pytest.raises(Exception, match="not configured"):
        await server.call_tool("things_run_shortcut", {"alias": "run-anything", "payload": {}})


async def test_stdio_protocol_initialization_discovery_and_readonly_error():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "things_mcp.server"],
        env={**os.environ, "THINGS_READ_ONLY": "true"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert "things_capabilities" in {tool.name for tool in tools.tools}
        caps = await session.call_tool("things_capabilities", {})
        assert not caps.isError
        assert caps.structuredContent["read_only"] is True
        blocked = await session.call_tool("things_delete", {"kind": "to-do", "item_id": "x"})
        assert blocked.isError
