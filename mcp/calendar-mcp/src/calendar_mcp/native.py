"""Fixed EventKit reads. No permission prompts, mutations, or private database access."""

import json
import os
import platform
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from .models import ID, EventQuery

AUTHORIZATION = {
    0: "not_determined",
    1: "restricted",
    2: "denied",
    3: "full_access",
    4: "write_only",
}
MAX_EVENTS = 10000


class CalendarError(Exception):
    """An error whose message is safe to return to the MCP client."""


def allowed_ids():
    try:
        return TypeAdapter(list[ID]).validate_json(os.environ.get("CALENDAR_ALLOWED_IDS", "[]"))
    except ValueError:
        raise CalendarError("Invalid calendar allowlist configuration.") from None


def select_calendars(calendars, allowed, requested=None):
    visible = {str(c.calendarIdentifier()): c for c in calendars}
    if allowed:
        if not set(allowed) <= visible.keys():
            raise CalendarError(
                "A configured calendar is unavailable. Check the calendar allowlist."
            )
        visible = {key: value for key, value in visible.items() if key in allowed}
    if requested is not None:
        if not set(requested) <= visible.keys():
            raise CalendarError("A requested calendar is unavailable or not allowed.")
        visible = {key: value for key, value in visible.items() if key in requested}
    return list(visible.values())


def timestamp(value):
    return datetime.fromtimestamp(value.timeIntervalSince1970(), UTC).isoformat()


def snapshot(event, include_notes=False):
    zone = event.timeZone()
    result = {
        "id": str(event.eventIdentifier() or ""),
        "calendar_id": str(event.calendar().calendarIdentifier()),
        "calendar_name": str(event.calendar().title()),
        "title": str(event.title() or "")[:4000],
        "start": timestamp(event.startDate()),
        "end": timestamp(event.endDate()),
        "time_zone": str(zone.name()) if zone else None,
        "all_day": bool(event.isAllDay()),
        "recurring": bool(event.hasRecurrenceRules()),
        "status": int(event.status()),
        "availability": int(event.availability()),
        "location": str(event.location() or "")[:4000],
        "url": str(event.URL().absoluteString())[:4000] if event.URL() else None,
    }
    if include_notes:
        notes = str(event.notes() or "")
        result.update(notes=notes[:8000], notes_truncated=len(notes) > 8000)
    return result


def event_page(events, query):
    # EventKit expands recurrence for the requested interval. Identify each occurrence
    # with both its ID and start timestamp; a series can reuse an event identifier.
    rows = [snapshot(event, query.include_notes) for event in events]
    rows = [
        r
        for r in rows
        if datetime.fromisoformat(r["start"]) < query.end
        and (
            datetime.fromisoformat(r["end"]) > query.start
            or query.start <= datetime.fromisoformat(r["start"]) < query.end
        )
    ]
    if query.text:
        needle = query.text.casefold()
        rows = [
            r for r in rows if needle in r["title"].casefold() or needle in r["location"].casefold()
        ]
    rows.sort(key=lambda r: (r["start"], r["end"], r["calendar_id"], r["id"]))
    stop = query.offset + query.limit
    return {
        "events": rows[query.offset : stop],
        "total": len(rows),
        "offset": query.offset,
        "next_offset": stop if stop < len(rows) else None,
        "start": query.start.isoformat(),
        "end": query.end.isoformat(),
    }


def handle(request, *, eventkit=None, foundation=None):
    if not isinstance(request, dict) or set(request) - {"operation", "query"}:
        raise CalendarError("Invalid native request.")
    operation = request.get("operation")
    if operation not in ("health", "list_calendars", "list_events"):
        raise CalendarError("Only calendar reads are supported.")
    if operation != "list_events" and "query" in request:
        raise CalendarError("This operation accepts no query.")
    query = EventQuery.model_validate(request.get("query")) if operation == "list_events" else None
    if eventkit is None:
        if platform.system() != "Darwin" or int(platform.mac_ver()[0].split(".")[0]) < 14:
            raise CalendarError("Calendar MCP requires macOS 14 or later.")
        import EventKit as eventkit
        import Foundation as foundation
    status = int(eventkit.EKEventStore.authorizationStatusForEntityType_(0))
    if operation == "health":
        return {
            "backend": "EventKit",
            "read_only": True,
            "authorization": AUTHORIZATION.get(status, "unknown"),
            "readable": status == 3,
        }
    if status != 3:
        raise CalendarError(
            "Calendar read access is unavailable (" + AUTHORIZATION.get(status, "unknown") + "). "
            "Allow Full Access for the host application in macOS System Settings > Privacy & "
            "Security > Calendars. This MCP only reads and never requests permission itself."
        )
    context = None
    if query is not None:
        now = datetime.now(UTC)
        zone = query.time_zone or str(foundation.NSTimeZone.localTimeZone().name())
        query = query.resolve(now, zone)
        context = {"current_time": now.astimezone(ZoneInfo(zone)).isoformat(), "time_zone": zone}
    store = eventkit.EKEventStore.alloc().init()
    calendars = select_calendars(
        store.calendarsForEntityType_(0), allowed_ids(), query.calendar_ids if query else None
    )
    if operation == "list_calendars":
        return {
            "calendars": [
                {
                    "id": str(c.calendarIdentifier()),
                    "title": str(c.title()),
                    "source": str(c.source().title()),
                }
                for c in calendars
            ]
        }
    # An empty array means all calendars to some APIs. Return before calling EventKit.
    if not calendars:
        return event_page([], query) | context
    start = foundation.NSDate.dateWithTimeIntervalSince1970_(query.start.timestamp())
    end = foundation.NSDate.dateWithTimeIntervalSince1970_(query.end.timestamp())
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(start, end, calendars)
    events = []

    def collect(event, stop):
        events.append(event)
        return len(events) > MAX_EVENTS

    store.enumerateEventsMatchingPredicate_usingBlock_(predicate, collect)
    if len(events) > MAX_EVENTS:
        raise CalendarError("Too many events. Use a shorter date range or fewer calendars.")
    return event_page(events, query) | context


def main():
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise CalendarError("Calendar request exceeds 64 KB.")
        result = {"result": handle(json.loads(raw))}
    except CalendarError as error:
        result = {"error": str(error)}
    except Exception:
        # Native exceptions and validation errors can contain user data.
        result = {
            "error": "Calendar read failed. Check the request, dependencies, and permissions."
        }
    output = json.dumps(result, ensure_ascii=False).encode()
    if len(output) > 2_000_000:
        output = b'{"error":"Calendar result exceeds 2 MB. Reduce the page size or omit notes."}'
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    main()
