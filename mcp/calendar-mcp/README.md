# Calendar MCP

Read calendars and events synced to Apple Calendar on the Mac. The server uses
Apple's public EventKit API. It is separate from the Things MCP and runs over
local stdio. It has no create, update, delete, or permission-request operation.

The tools are `calendar_capabilities`, `calendar_health`,
`calendar_list_calendars`, and `calendar_list_events`.

Use macOS 14 or later and Python 3.12 or later. Prepare the local environment
with `uv sync --locked --directory mcp/calendar-mcp` from the repository root.
The lockfile pins the MCP SDK and PyObjC EventKit dependencies.

Allow Full Access for the host application in macOS Privacy & Security > Calendars.
The server remains read-only and cannot request permission itself.
Enable `integrations.calendar.enabled` in the root `config.yaml`.
Set `calendar_ids` to allowed IDs, or use `[]` for all synced calendars.
Keep personal IDs out of public commits. Start the stack with `./run.sh`.

Run package checks with `uv run --locked --directory mcp/calendar-mcp pytest -q`.
The default tests use simulated calendar data and do not alter real calendars.
