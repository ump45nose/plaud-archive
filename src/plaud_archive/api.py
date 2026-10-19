from __future__ import annotations

import hmac
import os
import shutil
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from mcp_platform import RuntimeConfig, create_http_app
from pydantic import BaseModel, ConfigDict

from .mcp import create_mcp
from .settings import secret
from .store import Archive, Conflict, NotFound


class DayCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str
    markdown: str


class TextUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    markdown: str


class AudioUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


def create_app(archive=None, runtime=None, read_token=None, write_token=None):
    archive = archive or Archive(os.getenv("PLAUD_ARCHIVE_ROOT", "/data"))
    runtime = runtime or RuntimeConfig.from_env("plaud-archive")
    runtime.allowed_hosts.extend(("plaud-archive", "192.168.31.201"))
    read_token = read_token or secret("PLAUD_API_READ_TOKEN")
    write_token = write_token or secret("PLAUD_API_WRITE_TOKEN")
    if not read_token or not write_token or read_token == write_token: raise ValueError("distinct API read/write credentials required")
    server = create_mcp(archive, runtime)
    mcp_app = create_http_app(server, runtime)

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_app.lifespan(mcp_app): yield

    app = FastAPI(title="Plaud Archive", version="0.1.0", lifespan=lifespan)
    app.state.archive, app.state.mcp = archive, server

    def match(header, token): return bool(header) and hmac.compare_digest(header.encode(), ("Bearer " + token).encode())

    def read(authorization: str | None = Header(default=None)):
        if not any(match(authorization, t) for t in (read_token, write_token)): raise HTTPException(401, "invalid credential")

    def write(authorization: str | None = Header(default=None)):
        if not match(authorization, write_token):
            raise HTTPException(403 if match(authorization, read_token) else 401, "write permission required")

    @app.exception_handler(NotFound)
    async def missing(request, error): return JSONResponse({"detail": str(error)}, 404)

    @app.exception_handler(Conflict)
    async def conflict(request, error): return JSONResponse({"detail": str(error)}, 409)

    @app.exception_handler(ValueError)
    async def invalid(request, error): return JSONResponse({"detail": str(error)}, 422)

    @app.exception_handler(sqlite3.OperationalError)
    async def unavailable(request, error): return JSONResponse({"detail": "archive database unavailable"}, 503)

    @app.get("/healthz")
    def healthz():
        with archive.connect() as db: result = db.execute("SELECT 1").fetchone()[0]
        return {"status": "ok" if result == 1 else "error"}

    @app.get("/api/v1/status", dependencies=[Depends(read)])
    def status(date: str | None = None, limit: int = 50): return archive.status(date, limit)

    @app.get("/api/v1/archives", dependencies=[Depends(read)])
    def archives(start_date: str | None = None, end_date: str | None = None, offset: int = 0, limit: int = 50):
        return archive.list_days(start_date, end_date, offset, limit)

    @app.post("/api/v1/archives", dependencies=[Depends(write)], status_code=201)
    def create(record: DayCreate): return archive.create_day(record.date, record.markdown)

    @app.get("/api/v1/archives/{date}", dependencies=[Depends(read)])
    def get(date: str): return archive.get_day(date)

    @app.patch("/api/v1/archives/{date}", dependencies=[Depends(write)])
    def update(date: str, record: TextUpdate): return archive.update_day(date, record.markdown)

    @app.delete("/api/v1/archives/{date}", dependencies=[Depends(write)])
    def delete(date: str): return archive.delete_day(date)

    @app.get("/api/v1/archives/{date}/transcript.md", dependencies=[Depends(read)], response_class=PlainTextResponse)
    def transcript(date: str):
        value = archive.get_day(date)["markdown"]
        if value is None: raise NotFound("transcript not published")
        return PlainTextResponse(value, media_type="text/markdown", headers={"Content-Disposition": f'inline; filename="{date}.md"'})

    @app.get("/api/v1/archives/{date}/audio/{audio_date}/{name}", dependencies=[Depends(read)])
    def linked_audio(date: str, audio_date: str, name: str):
        # Resolve portable audio/<date>/<filename> links relative to transcript.md.
        if date != audio_date: raise NotFound("audio not found")
        row = archive.get_audio_by_name(date, name)
        return FileResponse(archive.audio_directory(date) / row["name"], filename=row["name"])

    @app.post("/api/v1/archives/{date}/audio", dependencies=[Depends(write)], status_code=201)
    def upload(date: str, file: UploadFile = File()):
        fd, name = tempfile.mkstemp(dir=archive.root)
        try:
            with os.fdopen(fd, "wb") as out: shutil.copyfileobj(file.file, out, 1024 * 1024)
            with archive.lock(date): return archive.add_audio(date, file.filename, Path(name))
        finally:
            Path(name).unlink(missing_ok=True); file.file.close()

    @app.get("/api/v1/archives/{date}/audio/{identifier}", dependencies=[Depends(read)])
    def audio(date: str, identifier: str):
        path = archive.audio_path(date, identifier)
        return FileResponse(path, filename=path.name)

    @app.patch("/api/v1/archives/{date}/audio/{identifier}", dependencies=[Depends(write)])
    def rename(date: str, identifier: str, record: AudioUpdate): return archive.update_audio(date, identifier, record.name)

    @app.delete("/api/v1/archives/{date}/audio/{identifier}", dependencies=[Depends(write)])
    def remove(date: str, identifier: str): return archive.delete_audio(date, identifier)

    app.mount("/", mcp_app)
    return app
