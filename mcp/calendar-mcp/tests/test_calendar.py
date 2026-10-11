import asyncio
import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from calendar_mcp.backend import CalendarBackend
from calendar_mcp.models import EventQuery
from calendar_mcp.native import CalendarError, event_page, handle, select_calendars
from calendar_mcp.server import create_server

RANGE = {"start": "2026-10-01T00:00:00-04:00", "end": "2026-10-08T00:00:00-04:00"}


def calendar(identifier):
    return SimpleNamespace(
        calendarIdentifier=lambda: identifier,
        title=lambda: identifier,
        source=lambda: SimpleNamespace(title=lambda: "Fixture account"),
    )


def event(identifier, start, end, *, recurring=False, all_day=False, title="Fixture"):
    def date(value):
        return SimpleNamespace(
            timeIntervalSince1970=lambda: datetime.fromisoformat(value).timestamp()
        )

    return SimpleNamespace(
        eventIdentifier=lambda: identifier,
        calendar=lambda: calendar("allowed"),
        title=lambda: title,
        startDate=lambda: date(start),
        endDate=lambda: date(end),
        timeZone=lambda: None,
        isAllDay=lambda: all_day,
        hasRecurrenceRules=lambda: recurring,
        status=lambda: 1,
        availability=lambda: 0,
        location=lambda: "Room",
        URL=lambda: None,
        notes=lambda: "private notes" * 1000,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"start": "2026-10-01T00:00:00"},
        {"end": RANGE["start"]},
        {"end": "2027-10-01T00:00:00Z"},
        {"calendar_ids": []},
        {"calendar_ids": ["x", "x"]},
        {"calendar_ids": ["\x00"]},
        {"limit": 201},
        {"offset": -1},
        {"delete": True},
    ],
)
def test_invalid_queries_rejected(change):
    with pytest.raises(ValidationError):
        EventQuery(**(RANGE | change))


def test_explicit_offsets_survive_dst_transition():
    query = EventQuery(start="2026-11-01T00:00:00-04:00", end="2026-11-02T00:00:00-05:00")
    assert query.end.timestamp() - query.start.timestamp() == 25 * 3600


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"period": "this_week", **RANGE},
        {"start": RANGE["start"]},
        {"period": "today", "time_zone": "invalid/zone"},
        {"period": "all_time"},
    ],
)
def test_ambiguous_or_invalid_relative_queries_rejected(data):
    with pytest.raises(ValidationError):
        EventQuery(**data)


@pytest.mark.parametrize(
    "period,now,start,end,hours",
    [
        (
            "today",
            "2026-11-01T16:00:00Z",
            "2026-11-01T00:00:00-04:00",
            "2026-11-02T00:00:00-05:00",
            25,
        ),
        (
            "today",
            "2026-03-08T16:00:00Z",
            "2026-03-08T00:00:00-05:00",
            "2026-03-09T00:00:00-04:00",
            23,
        ),
        (
            "this_week",
            "2026-11-01T16:00:00Z",
            "2026-10-26T00:00:00-04:00",
            "2026-11-02T00:00:00-05:00",
            169,
        ),
        (
            "next_week",
            "2026-12-31T16:00:00Z",
            "2027-01-04T00:00:00-05:00",
            "2027-01-11T00:00:00-05:00",
            168,
        ),
        (
            "tomorrow",
            "2026-10-11T02:00:00Z",
            "2026-10-11T00:00:00-04:00",
            "2026-10-12T00:00:00-04:00",
            24,
        ),
    ],
)
def test_relative_periods_use_local_dates_and_dst(period, now, start, end, hours):
    query = EventQuery(period=period, time_zone="America/New_York")
    resolved = query.resolve(datetime.fromisoformat(now), "UTC")
    assert resolved.start.isoformat() == start
    assert resolved.end.isoformat() == end
    assert resolved.end.timestamp() - resolved.start.timestamp() == hours * 3600
    assert EventQuery(**resolved.model_dump()) == resolved


def test_relative_query_defaults_to_host_zone_and_empty_results_keep_context(monkeypatch):
    monkeypatch.setenv("CALENDAR_ALLOWED_IDS", "[]")
    cls = Mock()
    cls.authorizationStatusForEntityType_.return_value = 3
    store = cls.alloc.return_value.init.return_value
    store.calendarsForEntityType_.return_value = []
    foundation = SimpleNamespace(
        NSTimeZone=SimpleNamespace(localTimeZone=lambda: SimpleNamespace(name=lambda: "Asia/Seoul"))
    )
    result = handle(
        {"operation": "list_events", "query": {"period": "today"}},
        eventkit=SimpleNamespace(EKEventStore=cls),
        foundation=foundation,
    )
    assert result["events"] == []
    assert result["time_zone"] == "Asia/Seoul"
    assert result["start"][:10] == result["current_time"][:10]
    assert result["start"].endswith("+09:00")
    store.predicateForEventsWithStartDate_endDate_calendars_.assert_not_called()


@pytest.mark.parametrize("status", [0, 1, 2, 4, 999])
def test_missing_read_permission_never_reads_or_returns_empty_success(status):
    store = Mock()
    store.authorizationStatusForEntityType_.return_value = status
    kit = SimpleNamespace(EKEventStore=store)
    assert handle({"operation": "health"}, eventkit=kit)["readable"] is False
    for request in ({"operation": "list_calendars"}, {"operation": "list_events", "query": RANGE}):
        with pytest.raises(CalendarError, match="read access is unavailable"):
            handle(request, eventkit=kit)
    store.alloc.assert_not_called()


def test_native_dispatch_rejects_mutations_and_extra_fields():
    kit = Mock()
    for operation in ("create", "update", "delete", "request_access", "saveEvent", "exec"):
        with pytest.raises(CalendarError, match="Only calendar reads"):
            handle({"operation": operation}, eventkit=kit)
    with pytest.raises(CalendarError):
        handle({"operation": "health", "script": "anything"}, eventkit=kit)
    assert not kit.mock_calls


def test_allowlist_cannot_be_widened_by_client():
    calendars = [calendar("allowed"), calendar("other")]
    assert [c.calendarIdentifier() for c in select_calendars(calendars, ["allowed"])] == ["allowed"]
    with pytest.raises(CalendarError, match="not allowed"):
        select_calendars(calendars, ["allowed"], ["other"])
    with pytest.raises(CalendarError, match="configured calendar"):
        select_calendars(calendars, ["missing"])


def test_occurrences_are_sorted_paginated_and_keep_exclusive_all_day_end():
    rows = [
        event("series", "2026-10-03T14:00:00Z", "2026-10-03T15:00:00Z", recurring=True),
        event("day", "2026-10-02T04:00:00Z", "2026-10-03T04:00:00Z", all_day=True),
        event("series", "2026-10-01T14:00:00Z", "2026-10-01T15:00:00Z", recurring=True),
        event("outside", "2026-10-08T04:00:00Z", "2026-10-08T05:00:00Z"),
    ]
    first = event_page(rows, EventQuery(**RANGE, limit=2))
    assert first["total"] == 3
    assert first["next_offset"] == 2
    assert first["events"][1]["all_day"]
    assert first["events"][1]["end"] == "2026-10-03T04:00:00+00:00"
    assert "notes" not in first["events"][0]
    second = event_page(rows, EventQuery(**RANGE, limit=2, offset=2, include_notes=True))
    assert second["next_offset"] is None
    assert second["events"][0]["id"] == first["events"][0]["id"]
    assert second["events"][0]["start"] != first["events"][0]["start"]
    assert second["events"][0]["notes_truncated"]
    assert len(second["events"][0]["notes"]) == 8000


def test_overlapping_and_zero_duration_events_are_included():
    rows = [
        event("overlap", "2026-09-30T04:00:00Z", "2026-10-02T04:00:00Z"),
        event("point", "2026-10-02T04:00:00Z", "2026-10-02T04:00:00Z"),
        event("ended", "2026-09-30T04:00:00Z", "2026-10-01T04:00:00Z"),
    ]
    page = event_page(rows, EventQuery(**RANGE))
    assert [r["id"] for r in page["events"]] == ["overlap", "point"]


def test_search_matches_title_or_location_not_notes():
    rows = [event("a", "2026-10-02T04:00:00Z", "2026-10-02T05:00:00Z", title="Meeting")]
    assert event_page(rows, EventQuery(**RANGE, text="MEET"))["total"] == 1
    assert event_page(rows, EventQuery(**RANGE, text="room"))["total"] == 1
    assert event_page(rows, EventQuery(**RANGE, text="private"))["total"] == 0


def test_native_query_uses_selected_calendars_and_eventkit_occurrences(monkeypatch):
    monkeypatch.setenv("CALENDAR_ALLOWED_IDS", '["allowed"]')
    store = Mock()
    store.calendarsForEntityType_.return_value = [calendar("allowed"), calendar("other")]
    fixture = event("series", "2026-10-02T04:00:00Z", "2026-10-02T05:00:00Z", recurring=True)
    store.enumerateEventsMatchingPredicate_usingBlock_.side_effect = lambda p, cb: cb(fixture, None)
    cls = Mock()
    cls.authorizationStatusForEntityType_.return_value = 3
    cls.alloc.return_value.init.return_value = store
    foundation = SimpleNamespace(
        NSDate=SimpleNamespace(dateWithTimeIntervalSince1970_=lambda t: t),
        NSTimeZone=SimpleNamespace(
            localTimeZone=lambda: SimpleNamespace(name=lambda: "America/New_York")
        ),
    )
    result = handle(
        {"operation": "list_events", "query": RANGE},
        eventkit=SimpleNamespace(EKEventStore=cls),
        foundation=foundation,
    )
    assert result["total"] == 1
    assert result["events"][0]["recurring"]
    assert result["events"][0]["calendar_name"] == "allowed"
    assert result["time_zone"] == "America/New_York"
    assert datetime.fromisoformat(result["current_time"]).tzinfo is not None
    start, end, calendars = store.predicateForEventsWithStartDate_endDate_calendars_.call_args.args
    assert datetime.fromtimestamp(start, UTC).hour == 4
    assert end > start
    assert [c.calendarIdentifier() for c in calendars] == ["allowed"]
    assert {c[0] for c in store.mock_calls} <= {
        "calendarsForEntityType_",
        "predicateForEventsWithStartDate_endDate_calendars_",
        "enumerateEventsMatchingPredicate_usingBlock_",
    }


async def test_mcp_exposes_only_read_tools_and_validates_before_backend():
    backend = AsyncMock()
    server = create_server(backend)
    tools = await server.list_tools()
    assert {t.name for t in tools} == {
        "calendar_capabilities",
        "calendar_health",
        "calendar_list_calendars",
        "calendar_list_events",
    }
    assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools)
    with pytest.raises(ToolError, match="end must follow start"):
        await server.call_tool("calendar_list_events", {"query": RANGE | {"end": RANGE["start"]}})
    backend.call.assert_not_called()
    backend.call.return_value = {"events": [], "total": 0}
    await server.call_tool("calendar_list_events", {"query": RANGE})
    assert backend.call.call_args.args[0] == "list_events"


async def test_real_stdio_discovery_capabilities_and_unknown_write():
    params = StdioServerParameters(command=sys.executable, args=["-m", "calendar_mcp.server"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert len((await session.list_tools()).tools) == 4
        caps = await session.call_tool("calendar_capabilities", {})
        assert not caps.isError
        assert caps.structuredContent["mutations"] is False
        assert (await session.call_tool("calendar_delete", {"id": "fixture"})).isError


async def test_backend_timeout_reaps_child_and_hides_stderr(monkeypatch):
    child = SimpleNamespace(
        stdin=Mock(drain=AsyncMock()),
        stdout=Mock(),
        returncode=None,
        wait=AsyncMock(),
        kill=Mock(),
    )

    async def blocked(_):
        await asyncio.sleep(60)

    child.stdout.read = blocked
    spawn = AsyncMock(return_value=child)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("PRIVATE_TOKEN", "must-not-reach-worker")
    backend = CalendarBackend()
    backend.timeout = 0.01
    with pytest.raises(ValueError, match="timed out"):
        await backend.call("health")
    child.kill.assert_called_once()
    child.wait.assert_awaited_once()
    assert "PRIVATE_TOKEN" not in spawn.call_args.kwargs["env"]
    assert spawn.call_args.kwargs["stderr"] == asyncio.subprocess.DEVNULL
    assert json.loads(child.stdin.write.call_args.args[0]) == {"operation": "health"}
