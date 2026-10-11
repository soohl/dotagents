"""Check cancellation against real owned and unrelated subprocesses."""
import asyncio
from pathlib import Path
import sys
import signal
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from jobs import OwnedJob


class JobTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancellation_during_spawn_keeps_and_reaps_the_child(self):
        real_spawn = asyncio.create_subprocess_exec
        spawned, release = asyncio.Event(), asyncio.Event()
        children = []
        async def delayed_spawn(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            children.append(process)
            spawned.set()
            await release.wait()
            return process
        job = OwnedJob(stop_timeout=.1)
        with patch('jobs.asyncio.create_subprocess_exec', side_effect=delayed_spawn):
            task = asyncio.create_task(job.run(
                [sys.executable, '-c', 'import time; time.sleep(60)'], lambda _: None, ROOT))
            try:
                await asyncio.wait_for(spawned.wait(), 5)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertFalse(job.running)
                self.assertIsNotNone(children[0].returncode)
            finally:
                release.set()
                for process in children:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()

    async def test_output_failure_still_reaps_the_process(self):
        job = OwnedJob(stop_timeout=.1)
        def broken_output(chunk):
            raise RuntimeError('output closed')
        with self.assertRaisesRegex(RuntimeError, 'output closed'):
            await job.run([sys.executable, '-u', '-c',
                           'import time; print("ready"); time.sleep(60)'], broken_output, ROOT)
        self.assertFalse(job.running)
        self.assertIsNotNone(job.process.returncode)

    async def test_unresponsive_owned_group_is_killed_within_the_shutdown_deadline(self):
        unrelated = await asyncio.create_subprocess_exec(
            sys.executable, '-c', 'import time; time.sleep(60)', start_new_session=True)
        job = OwnedJob(stop_timeout=.15)
        ready = asyncio.Event()
        child_code = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'
        parent_code = ('import signal,subprocess,sys,time; '
                       'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                       f'child=subprocess.Popen([sys.executable,"-c",{child_code!r}]); '
                       'time.sleep(60)')
        task = asyncio.create_task(job.run([sys.executable, '-u', '-c', parent_code],
                                           lambda chunk: ready.set(), ROOT))
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
            started = time.monotonic()
            await asyncio.wait_for(job.stop(), timeout=2)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(await task, -signal.SIGKILL)
            self.assertTrue(job.group_stopped)
            self.assertIsNone(unrelated.returncode)
        finally:
            await job.stop()
            await task
            unrelated.terminate()
            await unrelated.wait()

    async def test_stop_cleans_descendants_after_the_group_leader_exits(self):
        job = OwnedJob(stop_timeout=.15)
        ready = asyncio.Event()
        child_code = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'
        parent_code = ('import subprocess,sys; '
                       f'subprocess.Popen([sys.executable,"-c",{child_code!r}])')
        task = asyncio.create_task(job.run([sys.executable, '-u', '-c', parent_code],
                                           lambda chunk: ready.set(), ROOT))
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
            while job.process.returncode is None:
                await asyncio.sleep(.01)
            await asyncio.wait_for(job.stop(), timeout=2)
            self.assertEqual(await asyncio.wait_for(task, timeout=2), 0)
            self.assertTrue(job.group_stopped)
        finally:
            await job.stop()
            await task

    async def test_job_collects_output_and_exit_status(self):
        job = OwnedJob()
        output = []
        code = await job.run([sys.executable, '-c', 'print("ready")'], output.append, ROOT)
        self.assertEqual(code, 0)
        self.assertEqual(''.join(output), 'ready\n')
        self.assertFalse(job.running)

    async def test_stop_terminates_only_the_owned_job(self):
        unrelated = await asyncio.create_subprocess_exec(
            sys.executable, '-c', 'import time; time.sleep(60)', start_new_session=True)
        job = OwnedJob()
        ready = asyncio.Event()
        task = asyncio.create_task(job.run(
            [sys.executable, '-u', '-c', 'import time; print("ready"); time.sleep(60)'],
            lambda chunk: ready.set(), ROOT))
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
            with self.assertRaisesRegex(ValueError, 'already running'):
                await job.run([sys.executable, '-c', 'pass'], lambda chunk: None, ROOT)
            await job.stop()
            self.assertNotEqual(await asyncio.wait_for(task, timeout=5), 0)
            self.assertTrue(job.cancelled)
            self.assertIsNone(unrelated.returncode)
        finally:
            await job.stop()
            await task
            unrelated.terminate()
            await unrelated.wait()

    async def test_idle_stop_has_no_process_target(self):
        job = OwnedJob()
        await job.stop()
        self.assertIsNone(job.process)
        self.assertFalse(job.cancelled)


if __name__ == '__main__':
    unittest.main()
