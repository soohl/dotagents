"""Optional, operator-configured Shortcuts. No agent-selected executable or path."""

import asyncio
import json
import os
import platform
import tempfile
from pathlib import Path

from pydantic import Field

from .backend import AutomationError
from .models import Model, Title


class Shortcut(Model):
    name: Title
    description: Title


class ShortcutConfig(Model):
    shortcuts: dict[str, Shortcut] = Field(default_factory=dict, max_length=25)


def load_shortcuts() -> dict[str, Shortcut]:
    path = os.environ.get("THINGS_SHORTCUTS_CONFIG")
    if not path:
        return {}
    try:
        return ShortcutConfig.model_validate_json(Path(path).read_text()).shortcuts
    except (OSError, ValueError):
        raise ValueError(
            "Cannot load THINGS_SHORTCUTS_CONFIG. Check its path and schema."
        ) from None


async def run_shortcut(shortcut: Shortcut, payload: dict):
    if platform.system() != "Darwin":
        raise AutomationError("Shortcuts requires macOS.")
    data = json.dumps(payload, ensure_ascii=False).encode()
    if len(data) > 64000:
        raise ValueError("Shortcut input exceeds 64 KB")
    with tempfile.TemporaryDirectory(prefix="things-mcp-") as directory:
        source = Path(directory) / "input.json"
        destination = Path(directory) / "output.json"
        source.touch(mode=0o600)
        source.write_bytes(data)
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/shortcuts",
            "run",
            shortcut.name,
            "--input-path",
            str(source),
            "--output-path",
            str(destination),
            "--output-type",
            "public.json",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), 60)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AutomationError(
                "Shortcut timed out. Its effects are unknown; inspect before retrying."
            ) from None
        if process.returncode:
            raise AutomationError(
                "Shortcut failed. Its effects may be partial; inspect before retrying."
            )
        if not destination.exists():
            return {"status": "finished", "output": None, "verified": False}
        if destination.stat().st_size > 1000000:
            raise AutomationError(
                "Shortcut output exceeds 1 MB; effects may already have occurred."
            )
        try:
            output = json.loads(destination.read_bytes())
        except (ValueError, UnicodeDecodeError):
            raise AutomationError(
                "Shortcut must return JSON. Effects may already have occurred."
            ) from None
        return {"status": "finished", "output": output, "verified": False}
