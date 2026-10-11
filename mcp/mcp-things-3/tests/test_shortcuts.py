import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from things_mcp.shortcuts import Shortcut, load_shortcuts, run_shortcut


def test_config_requires_explicit_aliases_and_rejects_extra_fields(tmp_path, monkeypatch):
    config = tmp_path / "shortcuts.json"
    config.write_text(
        json.dumps(
            {
                "shortcuts": {
                    "read": {
                        "name": "Reviewed Workflow",
                        "description": "Read a fixture",
                        "executable": "/bin/sh",
                    }
                }
            }
        )
    )
    monkeypatch.setenv("THINGS_SHORTCUTS_CONFIG", str(config))
    with pytest.raises(ValueError, match="Cannot load"):
        load_shortcuts()


async def test_shortcut_uses_private_files_and_fixed_executable(monkeypatch):
    monkeypatch.setattr("things_mcp.shortcuts.platform.system", lambda: "Darwin")
    paths = []

    async def spawn(*args, **kwargs):
        assert args[:3] == ("/usr/bin/shortcuts", "run", "Reviewed Workflow")
        source = Path(args[args.index("--input-path") + 1])
        destination = Path(args[args.index("--output-path") + 1])
        paths.extend([source, destination])
        assert source.stat().st_mode & 0o777 == 0o600
        assert source.parent.stat().st_mode & 0o777 == 0o700
        assert json.loads(source.read_text()) == {"title": "private & shell $(data)"}
        assert not any("private" in value for value in args)
        destination.write_text('{"id":"fixture"}')
        result = AsyncMock()
        result.returncode = 0
        return result

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await run_shortcut(
        Shortcut(name="Reviewed Workflow", description="Fixture"),
        {"title": "private & shell $(data)"},
    )
    assert result["output"] == {"id": "fixture"}
    assert not result["verified"]
    assert all(not path.exists() for path in paths)
