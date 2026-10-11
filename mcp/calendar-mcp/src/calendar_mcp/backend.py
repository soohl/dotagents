"""Run a bounded native reader with private input on stdin."""

import asyncio
import json
import os
import sys


class CalendarBackend:
    timeout = 30

    def __init__(self):
        self.lock = asyncio.Lock()

    async def call(self, operation, query=None):
        if operation not in ("health", "list_calendars", "list_events"):
            raise ValueError("Only calendar reads are supported.")
        request = {"operation": operation}
        if query is not None:
            request["query"] = query.model_dump(mode="json")
        environment = {
            key: os.environ[key]
            for key in ("HOME", "USER", "PATH", "LANG", "CALENDAR_ALLOWED_IDS")
            if key in os.environ
        }
        async with self.lock:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "calendar_mcp.native",
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                async with asyncio.timeout(self.timeout):
                    process.stdin.write(json.dumps(request).encode())
                    await process.stdin.drain()
                    process.stdin.close()
                    chunks = []
                    size = 0
                    while chunk := await process.stdout.read(65536):
                        size += len(chunk)
                        if size > 2_000_000:
                            raise ValueError("Calendar result exceeds 2 MB.")
                        chunks.append(chunk)
                    await process.wait()
                if process.returncode:
                    raise ValueError(
                        "Calendar reader failed. Check native dependencies and permissions."
                    )
                try:
                    reply = json.loads(b"".join(chunks))
                except ValueError:
                    raise ValueError("Invalid Calendar reader response.") from None
                if not isinstance(reply, dict):
                    raise ValueError("Invalid Calendar reader response.")
                if "error" in reply:
                    raise ValueError(reply["error"])
                return reply["result"]
            except TimeoutError:
                raise ValueError("Calendar read timed out; no calendar data was changed.") from None
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
