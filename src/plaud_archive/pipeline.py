from __future__ import annotations

import asyncio
import bisect
import json
import math
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import tiktoken

from .audio import cut
from .collection import PlaudCollector
from .providers import Providers
from .store import Conflict, atomic_write, day_key


ENGINES = ("groq", "gemini", "sensevoice")


def error_text(error):
    if isinstance(error, httpx.HTTPStatusError): return "upstream HTTP " + str(error.response.status_code)
    if isinstance(error, httpx.RequestError): return type(error).__name__ + ": upstream connection failed"
    if isinstance(error, BaseExceptionGroup): return "; ".join(error_text(e) for e in error.exceptions)
    # Errors never include requests, signed audio URLs, headers or credentials.
    return (type(error).__name__ + ": " + str(error))[:500]


def paragraphs(value):
    if value.get("paragraphs"): return [p for p in value["paragraphs"] if p.get("text", "").strip()]
    return [{"text": text.strip()} for text in re.split(r"\n\s*\n", value["text"]) if text.strip()]


def materials(title, sources):
    """Use source paragraphs, never numeric voting or fixed text comparison windows."""
    rows = {engine: paragraphs(sources[engine]) for engine in ENGINES}
    reference = rows["gemini"] or rows["groq"] or rows["sensevoice"]
    if not reference: return []
    groups = [{e: [] for e in ENGINES} for _ in reference]
    starts = [p.get("start") for p in reference]
    timed = all(isinstance(t, (int, float)) for t in starts) and starts == sorted(starts)
    for engine, items in rows.items():
        for index, item in enumerate(items):
            if timed and isinstance(item.get("start"), (int, float)):
                bucket = max(0, min(len(groups) - 1, bisect.bisect_right(starts, item["start"]) - 1))
            else:
                bucket = min(len(groups) - 1, index * len(groups) // len(items))
            groups[bucket][engine].append(item["text"])
    return ["录音：" + title + "\n" + "\n".join(engine + "：\n" + "\n\n".join(values) for engine, values in g.items() if values) for g in groups]


def batches(items, max_tokens=256000, encoding=None):
    encoding = encoding or tiktoken.get_encoding("cl100k_base")
    encode = lambda text: encoding.encode(text, disallowed_special=())
    current, size = [], 0
    for item in items:
        length = len(encode(item)) + 2
        if length > max_tokens: raise ValueError("one source paragraph exceeds max_input_tokens")
        if current and size + length > max_tokens:
            yield "\n\n".join(current)
            current, size = [], 0
        current.append(item); size += length
    if current: yield "\n\n".join(current)


class Pending(Exception): pass


class Pipeline:
    def __init__(self, archive, config, providers=None, collector=None, clock=time.time):
        self.archive, self.config, self.clock = archive, config, clock
        self.providers = providers or Providers(config)
        self.collector = collector or PlaudCollector(config, archive)

    def step(self, day, key, path, operation):
        previous = self.archive.step(day, key)
        attempts = 0 if previous and previous["state"] == "complete" else previous["attempts"] if previous else 0
        if path.is_file():
            try: result = json.loads(path.read_text())
            except (ValueError, OSError) as error:
                self.archive.save_step(day, key, "failed", attempts + 1, retry_at=self.clock() + self.config.retry_seconds, error="RAW cache unreadable: " + path.name)
                raise Pending(key + ": RAW cache unreadable") from error
            if not previous or previous["state"] != "complete":
                self.archive.save_step(day, key, "complete", attempts, path=str(path.relative_to(self.archive.root)))
            return result
        if attempts >= self.config.max_attempts: raise Pending(key + ": attempts exhausted")
        if previous and previous["retry_at"] > self.clock(): raise Pending(key + ": retry scheduled")
        attempts += 1
        self.archive.save_step(day, key, "running", attempts, retry_at=self.clock() + self.config.retry_seconds)
        self.archive.job(day, "running", key)
        try:
            result = operation()
            atomic_write(path, json.dumps(result, ensure_ascii=False).encode())
        except Exception as error:
            detail = error_text(error)
            self.archive.save_step(day, key, "failed", attempts, retry_at=self.clock() + self.config.retry_seconds, error=detail)
            raise Pending(key + ": " + detail) from None
        self.archive.save_step(day, key, "complete", attempts, path=str(path.relative_to(self.archive.root)))
        return result

    def run_day(self, day, *, collect=True):
        day_key(day)
        with self.archive.lock(day):
            old = self.archive.status(day)
            if old and old[0]["state"] == "complete": return {"date": day, "state": "complete", "reused": True}
            self.archive.ensure_day(day)
            raw = self.archive.root / "raw" / day
            raw.mkdir(parents=True, exist_ok=True)
            try:
                if collect:
                    self.step(day, "collect", raw / "collection.json", lambda: asyncio.run(self.collector.collect(day)))
                archive = self.archive.get_day(day)
                if not archive["audio"]:
                    self.archive.job(day, "complete")
                    return {"date": day, "state": "complete", "audio_count": 0}
                inputs, day_failures = [], []
                for audio in archive["audio"]:
                    source = self.archive.audio_path(day, audio["id"])
                    duration = audio["duration_ms"] / 1000 if audio.get("duration_ms") else __import__("plaud_archive.audio", fromlist=["probe"]).probe(source)[1]
                    for block in range(math.ceil(duration / self.config.audio_chunk_seconds)):
                        folder = raw / audio["id"] / f"b{block:04d}"
                        folder.mkdir(parents=True, exist_ok=True)
                        offset = block * self.config.audio_chunk_seconds
                        seconds = min(self.config.audio_chunk_seconds, duration - offset)
                        clip = folder / "audio.mp3"
                        values, failures = {}, []
                        for engine in ENGINES:
                            def transcribe(engine=engine):
                                if not clip.is_file(): cut(source, clip, offset, seconds)
                                return getattr(self.providers, engine)(clip)
                            try:
                                values[engine] = self.step(day, f"{audio['id']}:{block}:{engine}", folder / f"{engine}.json", transcribe)
                            except Pending as error: failures.append(str(error))
                        if failures:
                            day_failures.extend(failures)
                            continue
                        inputs.extend(materials(Path(audio["name"]).stem, values))
                if day_failures: raise Pending("; ".join(day_failures))
                outputs = []
                for number, text in enumerate(batches(inputs, self.config.max_input_tokens)):
                    result = self.step(day, "fusion:" + str(number), raw / f"fusion-{number:04d}.json", lambda text=text: self.providers.fuse(text))
                    outputs.append(result["text"])
                if not outputs: raise RuntimeError("no fusion content")
                self.archive.publish(day, f"# {day}\n\n" + "\n\n".join(outputs).strip() + "\n")
                return {"date": day, "state": "complete", "audio_count": len(archive["audio"]), "fusion_batches": len(outputs)}
            except Exception as error:
                detail = str(error) if isinstance(error, Pending) else error_text(error)
                self.archive.job(day, "failed", error=detail)
                return {"date": day, "state": "failed", "error": detail}

    def retry(self, day):
        with self.archive.lock(day), self.archive.connect() as db:
            db.execute("UPDATE steps SET attempts=0,retry_at=0,state='failed' WHERE date=? AND state!='complete'", (day_key(day),))
            db.execute("UPDATE jobs SET state='queued',error=NULL WHERE date=?", (day,))

    def run_due(self, current=None):
        current = current or datetime.now(ZoneInfo(self.config.timezone))
        current = current.astimezone(ZoneInfo(self.config.timezone))
        yesterday = (current.date() - timedelta(days=1)).isoformat()
        with self.archive.connect() as db:
            if current.strftime("%H:%M") >= self.config.start_time and not db.execute("SELECT 1 FROM jobs WHERE date=?", (yesterday,)).fetchone():
                self.archive.job(yesterday, "queued")
            jobs = db.execute("SELECT date FROM jobs WHERE state!='complete' ORDER BY date").fetchall()
        result = []
        for row in jobs:
            status = self.archive.status(row["date"])[0]
            incomplete = [s for s in status["steps"] if s["state"] != "complete"]
            if status["state"] == "failed" and not incomplete: continue
            if any(s["attempts"] >= self.config.max_attempts for s in incomplete): continue
            if any(s["retry_at"] > self.clock() for s in incomplete): continue
            try: result.append(self.run_day(row["date"]))
            except Conflict: continue
        self.archive.cleanup_raw(self.config.raw_retention_days, self.clock())
        return result
