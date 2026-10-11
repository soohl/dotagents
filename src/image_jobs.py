"""Bounded image generation jobs. Saved sessions belong to ImageHistory."""
import asyncio
import base64
import json
import logging
from pathlib import Path
import secrets
import tempfile

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .image_backend import SETTINGS
from .async_tasks import settle
from .image_edit import annotate_edit, composite_edit, png, prepare_edit


ACTIVE = {"queued", "generating"}
ERROR_LIMIT = 64


class AreaEdit(BaseModel):
    base: int = Field(ge=0, le=9, strict=True)
    mask: str = Field(min_length=1)
    feather: int = Field(default=12, ge=0, le=40, strict=True)


class Generation(BaseModel):
    session: str | None = None
    model: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=4000)
    size: str = Field(pattern=r"^\d{2,5}x\d{2,5}$")
    steps: int = Field(ge=1, le=SETTINGS['max_steps'], strict=True)
    seed: int = Field(ge=-1, le=4294967295, strict=True)
    references: list[str] = Field(default_factory=list, max_length=10)
    edit: AreaEdit | None = None


class ImageJobs:
    def __init__(self, store, runtime, upstream):
        self.store = store
        self.runtime = runtime
        self.upstream = upstream
        self.jobs = {}
        self.tasks = set()
        self.preparations = set()
        self.gpu_queue = asyncio.Semaphore(1)
        self.closing = False

    async def submit(self, key, body):
        if self.closing:
            raise HTTPException(503, "Image service is shutting down.")
        if self.jobs.get(key, {}).get("status") in ACTIVE:
            raise HTTPException(409, "This session already has a generation in progress.")
        if sum(job['status'] in ACTIVE for job in self.jobs.values()) >= SETTINGS['queue']:
            raise HTTPException(429, "The image queue is full. Please try again shortly.")
        # Reserve before decoding edit images. Concurrent validation also uses memory.
        previous = self.jobs.pop(key, None)
        self.jobs[key] = dict(status="queued", prompt=body.prompt)
        try:
            prepared = None
            if body.edit:
                preparation = asyncio.create_task(asyncio.to_thread(
                    prepare_edit, body.references, body.edit.base, body.edit.mask, body.prompt))
                self.preparations.add(preparation)
                preparation.add_done_callback(self.preparations.discard)
                # A thread cannot be cancelled. Keep its slot until it releases images.
                prepared, cancelled = await settle(preparation)
                if cancelled:
                    raise asyncio.CancelledError
            if self.closing:
                raise HTTPException(503, "Image service is shutting down.")
            task = asyncio.create_task(self.generate(key, body, prepared))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        except BaseException:
            self.jobs.pop(key, None)
            if previous and not self.closing:
                self.jobs[key] = previous
            raise

    def failed(self, key, message):
        job = self.jobs.pop(key)
        self.jobs[key] = dict(job, status="error", error=message[:1024])
        errors = [key for key, job in self.jobs.items() if job['status'] not in ACTIVE]
        for old in errors[:-ERROR_LIMIT]:
            del self.jobs[old]

    async def generate(self, key, body, prepared):
        try:
            async with self.gpu_queue:
                self.jobs[key]['status'] = 'generating'
                with tempfile.TemporaryDirectory(dir=self.runtime) as directory:
                    references = []
                    for index, value in enumerate(body.references):
                        path = Path(directory) / f'reference-{index}.png'
                        path.write_bytes(base64.b64decode(value, validate=True))
                        references.append(str(path))
                    payload = body.model_dump(exclude={'session', 'edit'})
                    mask_bytes = None
                    if prepared:
                        base, mask, instruction = prepared
                        mask_bytes = png(mask)
                        payload['references'][body.edit.base] = base64.b64encode(annotate_edit(base, mask)).decode()
                        payload['prompt'] = instruction
                    if payload['seed'] == -1:
                        payload['seed'] = secrets.randbelow(2**32)
                    result = await self.upstream('POST', '/images/generate', json.dumps(payload).encode())
                    if not result.is_success:
                        detail = result.json().get('detail')
                        raise RuntimeError(detail if isinstance(detail, str) else 'Image generation failed.')
                    result = result.json()
                    entry = {k: payload[k] for k in ('model', 'prompt', 'size', 'steps', 'seed')}
                    entry['prompt'] = body.prompt
                    entry['status'] = result['status']
                    output = base64.b64decode(result['image'], validate=True)
                    if prepared:
                        composition = asyncio.create_task(asyncio.to_thread(
                            composite_edit, output, base, mask,
                            tuple(map(int, body.size.split('x'))), body.edit.feather))
                        output, cancelled = await settle(composition)
                        if cancelled:
                            raise asyncio.CancelledError
                        entry['edit'] = {'base': body.edit.base, 'feather': body.edit.feather}
                    self.store.append(key, entry, output, references, mask=mask_bytes)
                    # Completed state comes from the durable manifest, not a growing cache.
                    self.jobs.pop(key)
        except asyncio.CancelledError:
            self.failed(key, 'Generation was interrupted. Please try again.')
            raise
        except RuntimeError as error:
            self.failed(key, str(error))
        except Exception as error:
            # Malformed worker responses must also release the queue and become errors.
            logging.getLogger(__name__).error('Image generation failed (%s)', type(error).__name__)
            self.failed(key, 'Image inference is unavailable. Check the server, then try again.')

    async def close(self):
        self.closing = True
        await asyncio.gather(*list(self.preparations), return_exceptions=True)
        self.preparations.clear()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.jobs.clear()
