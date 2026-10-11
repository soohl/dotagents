"""Run and stop only a process group created by this TUI."""
import asyncio
import codecs
import os
import signal


from async_tasks import settle


class OwnedJob:
    def __init__(self, stop_timeout=5):
        self.process = None
        self.running = False
        self.cancelled = False
        self.started = asyncio.Event()
        self.stop_timeout = stop_timeout
        self.stop_lock = asyncio.Lock()
        self.group_stopped = False

    async def run(self, args, output, cwd):
        if self.running:
            raise ValueError('A terminal operation is already running.')
        self.running = True
        self.cancelled = False
        self.started.clear()
        self.process = None
        self.group_stopped = False
        cancelled = False
        try:
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *args, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True))
            self.process, cancelled = await settle(spawn)
            self.started.set()
            if cancelled:
                raise asyncio.CancelledError
            if self.cancelled:
                await self._terminate()
            decoder = codecs.getincrementaldecoder('utf-8')('replace')
            while chunk := await self.process.stdout.read(16384):
                output(decoder.decode(chunk))
            remainder = decoder.decode(b'', final=True)
            if remainder:
                output(remainder)
            return await self.process.wait()
        finally:
            self.started.set()
            try:
                if self.process is not None and not self.group_stopped:
                    _, interrupted = await settle(asyncio.create_task(self._terminate()))
                    cancelled |= interrupted
            finally:
                self.running = False
            if cancelled:
                raise asyncio.CancelledError

    async def _terminate(self):
        async with self.stop_lock:
            await self._stop_group()

    async def _stop_group(self):
        process = self.process
        if process is None or self.group_stopped:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=self.stop_timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            # A leader can exit while an owned descendant is still alive.
            # Finish the isolated group even when the leader has already exited.
            self.group_stopped = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()

    async def stop(self):
        if not self.running:
            return
        self.cancelled = True
        await self.started.wait()
        _, cancelled = await settle(asyncio.create_task(self._terminate()))
        if cancelled:
            raise asyncio.CancelledError
