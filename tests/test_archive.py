import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from mcp_platform import RuntimeConfig

from plaud_archive.api import create_app
from plaud_archive.pipeline import Pipeline, batches
from plaud_archive.settings import Workflow
from plaud_archive.store import Archive, Conflict, NotFound, atomic_write


@pytest.fixture
def archive(tmp_path): return Archive(tmp_path)


def test_file_crud_and_deletion_does_not_resurrect(archive, tmp_path):
    day = "2026-10-17"
    text = "# 对话\n\n**甲：** 明天交付。\n"
    archive.create_day(day, text)
    with pytest.raises(Conflict): archive.create_day(day, text)
    source = tmp_path / "input.mp3"; source.write_bytes(b"audio")
    with archive.lock(day): a = archive.add_audio(day, "09-00.mp3", source)
    assert archive.get_day(day)["markdown"] == text
    archive.update_audio(day, a["id"], "10-00.mp3")
    assert archive.audio_path(day, a["id"]).read_bytes() == b"audio"
    archive.update_day(day, "改稿")
    archive.delete_day(day)
    assert archive.status(day)[0]["state"] == "complete"
    with pytest.raises(NotFound): archive.get_day(day)
    with archive.connect() as db:
        assert "markdown" not in [r[1] for r in db.execute("PRAGMA table_info(days)")]


def test_cleanup_only_raw(archive):
    archive.create_day("2026-10-01", "成品")
    raw = archive.root / "raw/2026-10-01/raw.json"
    atomic_write(raw, b"raw"); os.utime(raw, (0, 0))
    assert archive.cleanup_raw(30)["removed_files"] == 1
    assert archive.get_day("2026-10-01")["markdown"] == "成品"


def test_day_write_lock(archive):
    archive.create_day("2026-10-01", "a")
    with archive.lock("2026-10-01"):
        with pytest.raises(Conflict): archive.update_day("2026-10-01", "b")
    assert archive.get_day("2026-10-01")["markdown"] == "a"


def runtime():
    return RuntimeConfig(name="plaud-archive", development_no_auth=True, host="127.0.0.1")


def test_http_permissions_audio_and_mcp_equivalence(archive, tmp_path):
    app = create_app(archive, runtime(), "reader", "writer")
    with TestClient(app) as client:
        assert client.get("/api/v1/archives").status_code == 401
        record = {"date": "2026-10-17", "markdown": "正文"}
        assert client.post("/api/v1/archives", json=record, headers={"Authorization": "Bearer reader"}).status_code == 403
        assert client.post("/api/v1/archives", json=record, headers={"Authorization": "Bearer writer"}).status_code == 201
        assert client.get("/api/v1/archives/2026-10-17/transcript.md", headers={"Authorization": "Bearer reader"}).text == "正文"
        uploaded = client.post("/api/v1/archives/2026-10-17/audio", files={"file": ("a.mp3", b"audio", "audio/mpeg")}, headers={"Authorization": "Bearer writer"})
        assert uploaded.status_code == 201
        audio_id = uploaded.json()["id"]
        url = f"/api/v1/archives/2026-10-17/audio/{audio_id}"
        assert client.get(url, headers={"Authorization": "Bearer reader"}).content == b"audio"
        assert client.patch(url, json={"name": "b.mp3"}, headers={"Authorization": "Bearer writer"}).json()["name"] == "b.mp3"
        async def mcp():
            async with Client(app.state.mcp) as c:
                text = await c.call_tool("get_transcript", {"date": record["date"]})
                assert text.content[0].text == "正文"
                await c.call_tool("update_archive", {"date": record["date"], "markdown": "MCP 改稿"})
                assert archive.get_day(record["date"])["markdown"] == "MCP 改稿"
        asyncio.run(mcp())
        assert client.delete(url, headers={"Authorization": "Bearer writer"}).status_code == 200
        assert client.get(url, headers={"Authorization": "Bearer reader"}).status_code == 404


class FakeProviders:
    def __init__(self): self.calls = []; self.fail = True
    def groq(self, audio): self.calls.append("groq"); return {"text": "甲：你好。\n\n乙：明天交付。"}
    def gemini(self, audio):
        self.calls.append("gemini")
        if self.fail: raise RuntimeError("upstream unavailable")
        return {"text": "甲：你好。\n\n乙：明天交付。"}
    def sensevoice(self, audio): self.calls.append("sensevoice"); return {"text": "明天交付。"}
    def fuse(self, value): self.calls.append("fuse"); return {"text": "**甲：** 你好。\n\n**乙：** 明天交付。"}


def test_bounded_retry_resume_and_no_partial_publish(archive, tmp_path):
    day = "2026-10-17"; archive.ensure_day(day)
    source = tmp_path / "input.mp3"; source.write_bytes(b"mock")
    with archive.lock(day): archive.add_audio(day, source.name, source, duration_ms=1000)
    clock = [1000.0]; providers = FakeProviders()
    pipeline = Pipeline(archive, Workflow(str(tmp_path), retry_seconds=30, max_attempts=2), providers, clock=lambda: clock[0])
    with patch("plaud_archive.pipeline.cut", side_effect=lambda src, dst, off, dur: atomic_write(dst, b"clip")):
        assert pipeline.run_day(day, collect=False)["state"] == "failed"
        assert archive.get_day(day)["markdown"] is None
        assert providers.calls == ["groq", "gemini", "sensevoice"]
        pipeline.run_day(day, collect=False)
        assert providers.calls == ["groq", "gemini", "sensevoice"]
        clock[0] += 31
        assert pipeline.run_day(day, collect=False)["state"] == "failed"
        clock[0] += 31; pipeline.run_day(day, collect=False)
        assert providers.calls.count("gemini") == 2
        assert providers.calls.count("groq") == 1
        pipeline.retry(day); providers.fail = False
        assert pipeline.run_day(day, collect=False)["state"] == "complete"
        assert providers.calls.count("sensevoice") == 1
        assert "明天交付" in archive.get_day(day)["markdown"]
        pipeline.run_day(day, collect=False)
        assert providers.calls.count("fuse") == 1


def test_whole_paragraph_token_packing():
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    paras = ["一段文字。" * 5, "第二段文字。" * 5, "第三段文字。" * 5]
    out = list(batches(paras, 65, enc))
    assert all(len(enc.encode(text)) <= 65 for text in out)
    assert "\n\n".join(out) == "\n\n".join(paras)
    with pytest.raises(ValueError): list(batches(["very long paragraph" * 100], 3, enc))


def test_long_audio_tail_and_multiple_recording_order(archive, tmp_path):
    day = "2026-10-17"; archive.ensure_day(day)
    for start, duration in (("01", 1225000), ("02", 1000)):
        p = tmp_path / (start + ".mp3"); p.write_bytes(b"audio")
        with archive.lock(day): archive.add_audio(day, p.name, p, start_at=start, duration_ms=duration)
    provider = FakeProviders(); provider.fail = False
    spans = []
    def extract(source, path, offset, seconds): spans.append((source.name, offset, seconds)); atomic_write(path, b"clip")
    with patch("plaud_archive.pipeline.cut", side_effect=extract):
        assert Pipeline(archive, Workflow(str(tmp_path)), provider).run_day(day, collect=False)["state"] == "complete"
    assert spans == [("01.mp3", 0, 1200), ("01.mp3", 1200, 25.0), ("02.mp3", 0, 1.0)]
