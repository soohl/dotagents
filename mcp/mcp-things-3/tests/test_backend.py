import asyncio
import json
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from things_mcp.backend import AppleScript, AutomationError, literal
from things_mcp.models import Fields, Query


@pytest.mark.skipif(sys.platform != "darwin", reason="AppleScript string round trip needs macOS")
@pytest.mark.parametrize(
    "value",
    [
        '"\nend tell\ndo shell script "touch /tmp/never-run"\n--',
        '雪 😀 \\ & "\r\n\t end',
        "",
        "line\u2028separator",
    ],
)
def test_applescript_literal_roundtrip(value):
    backend = AppleScript()
    script = backend.prelude + "\nreturn my jsonText({" + literal(value) + "})"
    result = subprocess.run(
        ["/usr/bin/osascript", "-"], input=script, text=True, capture_output=True, check=True
    )
    assert json.loads(result.stdout) == [value]


async def test_error_diagnostics_do_not_leak_data_or_tokens(monkeypatch):
    monkeypatch.setattr("things_mcp.backend.platform.system", lambda: "Darwin")
    process = AsyncMock()
    process.returncode = 1
    process.communicate.return_value = (b"", b"secret-token and private notes (-1743)\n")
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(AutomationError, match="Automation") as error:
        await AppleScript().run("private script")
    assert "secret-token" not in str(error.value)
    assert "private notes" not in str(error.value)
    assert spawn.call_args.args == ("/usr/bin/osascript", "-")
    assert process.communicate.call_args.args[0].endswith(b"private script")


async def test_timeout_kills_child_and_does_not_retry(monkeypatch):
    monkeypatch.setattr("things_mcp.backend.platform.system", lambda: "Darwin")
    process = AsyncMock()
    process.returncode = None
    process.kill = lambda: setattr(process, "returncode", -9)
    process.communicate.side_effect = TimeoutError
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(AutomationError, match="outcome may be partial or unknown"):
        await AppleScript().run("anything")
    assert process.returncode == -9
    spawn.assert_awaited_once()
    process.wait.assert_awaited_once()


async def test_invalid_creation_never_reaches_applescript():
    backend = AppleScript()
    backend.run = AsyncMock()
    with pytest.raises(ValueError):
        await backend.create("tag", Fields(title="tag", notes="unsupported"))
    backend.run.assert_not_awaited()


async def test_search_values_are_quoted():
    backend = AppleScript()
    backend.run = AsyncMock(return_value={})
    text = '" or true --'
    await backend.query(Query(text=text))
    script = backend.run.call_args.args[0]
    assert f"name contains {literal(text)}" in script
