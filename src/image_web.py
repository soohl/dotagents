"""Image workspace adapted from soohl/cook-4090; see web/LICENSE.cook-4090."""

import base64
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import tempfile
import uuid

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import ValidationError

from .image_history import ImageHistory
from .image_jobs import Generation, ImageJobs
from .image_backend import SETTINGS, upstream

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "web/dist"


def create_app(history_directory=None):
    @asynccontextmanager
    async def lifespan(app):
        with tempfile.TemporaryDirectory(prefix="dotagents-image-web-") as directory:
            runtime = Path(directory)
            history = history_directory or os.environ.get("IMAGE_HISTORY_DIR") or runtime / "history"
            app.state.images = ImageJobs(ImageHistory(history), runtime, upstream)
            app.state.epoch = uuid.uuid4().hex
            try:
                yield
            finally:
                await app.state.images.close()

    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]'])
    application.middleware("http")(headers)
    application.include_router(routes)
    application.mount("/assets", StaticFiles(directory=FRONTEND / "assets", check_dir=False), name="assets")
    return application


routes = APIRouter()


async def headers(request, call_next):
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        return Response(status_code=403)
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("sec-fetch-site") not in {None, "same-origin"}:
        return Response(status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self'; img-src 'self' data: blob:; "
        "font-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    return response


def with_epoch(payload, epoch):
    if "epoch" in payload:
        payload["epoch"] += ":" + epoch
    return payload


@routes.get("/healthz")
async def healthz():
    return {"status": "ok"}


@routes.get("/api/options")
@routes.get("/api/health")
async def inference(request: Request):
    operation = request.url.path.rsplit("/", 1)[-1]
    target = {"options": "/images/options", "health": "/health"}.get(operation)
    if target is None:
        raise HTTPException(404)
    try:
        result = await upstream("GET", target)
        return Response(json.dumps(with_epoch(result.json(), request.app.state.epoch)), result.status_code, media_type="application/json")
    except (httpx.HTTPError, ValueError, KeyError):
        raise HTTPException(503, "Image inference is unavailable. Check the configured server.") from None


def session_entries(images, key):
    store, jobs = images.store, images.jobs
    try:
        entries = store.read(key)
    except ValueError:
        raise HTTPException(404) from None
    if not entries and key not in jobs:
        raise HTTPException(404)
    return entries


def public_session(images, key):
    jobs = images.jobs
    entries = session_entries(images, key)
    return {"id": key, "status": jobs.get(key, {}).get("status", "idle"),
            "error": jobs.get(key, {}).get("error"),
            "entries": [dict({k: e[k] for k in ("prompt", "model", "size", "steps", "seed", "status")},
                             image=f"/api/sessions/{key}/images/{i}",
                             edit={"base": e["edit"]["base"], "feather": e["edit"].get("feather", 12), "mask": f"/api/sessions/{key}/masks/{i}"} if e.get("edit") else None,
                             references=[f"/api/sessions/{key}/references/{i}/{j}" for j in range(len(e["references"]))])
                        for i, e in enumerate(entries)]}


@routes.get("/api/sessions")
async def list_sessions(request: Request):
    store, jobs = request.app.state.images.store, request.app.state.images.jobs
    saved = dict((key, title) for title, key in store.choices())
    keys = list(dict.fromkeys([*reversed(jobs), *saved]))
    return [{"id": key, "title": saved.get(key, jobs.get(key, {}).get("prompt", "New session"))[:60],
             "status": jobs.get(key, {}).get("status", "idle")} for key in keys]


# A dedicated route avoids exposing filesystem paths in session metadata.
@routes.get("/api/sessions/{key}")
async def get_session(key: str, request: Request):
    return public_session(request.app.state.images, key)


@routes.delete("/api/sessions/{key}", status_code=204)
async def delete_session(key: str, request: Request):
    images = request.app.state.images
    store, jobs = images.store, images.jobs
    session_entries(images, key)
    if jobs.get(key, {}).get("status") in {"queued", "generating"}:
        raise HTTPException(409, "Wait for this generation to finish before deleting the session.")
    store.delete(key)
    jobs.pop(key, None)
    return Response(status_code=204)


def saved_file(images, key, index, reference=None, mask=False):
    store = images.store
    entries = session_entries(images, key)
    try:
        if index < 0 or (reference is not None and reference < 0):
            raise IndexError()
        value = entries[index]["edit"]["mask"] if mask else entries[index]["image"] if reference is None else entries[index]["references"][reference]
        path = Path(value).resolve()
        if not path.is_relative_to(store.directory(key).resolve()) or not path.is_file():
            raise IndexError()
    except (IndexError, KeyError, TypeError, ValueError):
        raise HTTPException(404) from None
    return FileResponse(path, filename=path.name, content_disposition_type="inline")


@routes.get("/api/sessions/{key}/images/{index}")
async def image_file(key: str, index: int, request: Request):
    return saved_file(request.app.state.images, key, index)


@routes.get("/api/sessions/{key}/references/{index}/{reference}")
async def reference_file(key: str, index: int, reference: int, request: Request):
    return saved_file(request.app.state.images, key, index, reference)


@routes.get("/api/sessions/{key}/masks/{index}")
async def mask_file(key: str, index: int, request: Request):
    return saved_file(request.app.state.images, key, index, mask=True)


@routes.post("/api/generate", status_code=202)
async def start_generation(request: Request):
    images = request.app.state.images
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > int(os.environ.get("STACK_MAX_BODY", SETTINGS['max_body'])):
            raise HTTPException(413)
    try:
        body = Generation.model_validate_json(raw)
        if body.model != SETTINGS['model'] or body.size not in SETTINGS['sizes']:
            raise ValueError()
        if not body.prompt.strip():
            raise ValueError()
        key = body.session or uuid.uuid4().hex
        images.store.directory(key)
        for ref in body.references:
            base64.b64decode(ref, validate=True)
    except (ValidationError, ValueError):
        raise HTTPException(400, "Check your prompt, settings, and reference images.") from None
    try:
        await images.submit(key, body)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return {"id": key}


@routes.get("/")
async def index():
    if not (FRONTEND / "index.html").is_file():
        raise HTTPException(503, "The web interface has not been built.")
    return FileResponse(FRONTEND / "index.html")


app = create_app()


def main():
    import uvicorn
    from .image_backend import backend_url
    backend_url()
    uvicorn.run(app, host="0.0.0.0", port=8080,
                access_log=False, proxy_headers=False, limit_concurrency=64)


if __name__ == "__main__":
    main()
