# Things 3 MCP

A local MCP server for Things 3 on macOS. It uses public AppleScript commands
and the Things URL scheme. It does not access the Things database or Things Cloud.

Requires macOS, Things 3, Python 3.12 or later, and [uv](https://docs.astral.sh/uv/).

Prepare the environment from the repository root with
`uv sync --locked --directory mcp/mcp-things-3`.
Open Things and allow the launching application to control it in macOS Automation.
Set `DOTAGENTS_THINGS_TOKEN` in the private root `.env` to enable the Chat bridge.
Start the stack with `./run.sh`.

The MCP server uses stdio. DotAgents provides a separate authenticated local bridge.
The tools can change Things data. Set `THINGS_READ_ONLY=true` in the MCP server
environment to block write, UI, and Shortcut tools. Keep URL tokens private.
Connected clients may send retrieved data to their selected model provider.
