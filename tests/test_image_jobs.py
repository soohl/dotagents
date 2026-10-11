"""Image queues retain no completed payloads and close their owned tasks."""
import asyncio
import base64
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
import httpx

from src.image_history import ImageHistory
from src.image_jobs import AreaEdit, ERROR_LIMIT, Generation, ImageJobs


class ImageJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.upstream = AsyncMock(return_value=httpx.Response(200, json={
            'image': base64.b64encode(b'output').decode(), 'status': 'done'}))
        self.manager = ImageJobs(ImageHistory(self.root / 'history'), self.root, self.upstream)
        self.body = Generation(model='qwen-image-2.1', prompt='p', size='1024x1024', steps=1, seed=42)
        self.addAsyncCleanup(self.manager.close)

    async def drain(self):
        await asyncio.gather(*list(self.manager.tasks))
        await asyncio.sleep(0)  # Run the task retirement callbacks.

    async def test_completed_sessions_persist_without_retaining_jobs_or_tasks(self):
        for index in range(80):
            key = f'{index:032x}'
            await self.manager.submit(key, self.body)
            await self.drain()
            self.assertFalse(self.manager.jobs)
            self.assertFalse(self.manager.tasks)
        self.assertEqual(len(self.manager.store.choices()), 80)
        self.assertEqual(self.manager.store.read(f'{0:032x}')[0]['prompt'], 'p')

    async def test_malformed_responses_release_the_queue_and_bound_errors(self):
        self.upstream.return_value = httpx.Response(200, json=[])
        with self.assertLogs('src.image_jobs', level='ERROR'):
            for index in range(ERROR_LIMIT + 5):
                await self.manager.submit(f'{index:032x}', self.body)
                await self.drain()
        self.assertEqual(len(self.manager.jobs), ERROR_LIMIT)
        self.assertTrue(all(job['status'] == 'error' for job in self.manager.jobs.values()))
        self.upstream.return_value = httpx.Response(200, json={
            'image': base64.b64encode(b'output').decode(), 'status': 'done'})
        await self.manager.submit('f' * 32, self.body)
        await self.drain()
        self.assertEqual(len(self.manager.store.read('f' * 32)), 1)

    async def test_close_cancels_queued_and_running_tasks_and_rejects_new_work(self):
        started = asyncio.Event()
        async def blocked(*args):
            started.set()
            await asyncio.Event().wait()
        self.upstream.side_effect = blocked
        await self.manager.submit('a' * 32, self.body)
        await asyncio.wait_for(started.wait(), 2)
        await self.manager.submit('b' * 32, self.body)
        tasks = list(self.manager.tasks)
        await asyncio.wait_for(self.manager.close(), 2)
        self.assertTrue(all(task.done() for task in tasks))
        self.assertFalse(self.manager.tasks)
        self.assertFalse(self.manager.jobs)
        self.assertEqual(list(self.root.glob('tmp*')), [])
        with self.assertRaises(HTTPException) as error:
            await self.manager.submit('c' * 32, self.body)
        self.assertEqual(error.exception.status_code, 503)

    async def test_edit_validation_reserves_capacity_before_decoding_and_releases_on_error(self):
        entered, release = threading.Event(), threading.Event()
        def prepare(*args):
            entered.set()
            release.wait(3)
            raise ValueError('Invalid selection')
        body = self.body.model_copy(update={'edit': AreaEdit(base=0, mask='mask')})
        with patch('src.image_jobs.SETTINGS', {'queue': 1}), patch('src.image_jobs.prepare_edit', side_effect=prepare):
            task = asyncio.create_task(self.manager.submit('a' * 32, body))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                with self.assertRaises(HTTPException) as error:
                    await self.manager.submit('b' * 32, self.body)
                self.assertEqual(error.exception.status_code, 429)
                release.set()
                with self.assertRaisesRegex(ValueError, 'Invalid selection'):
                    await task
                self.assertFalse(self.manager.jobs)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
