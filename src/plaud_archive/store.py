from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from .transcript import change_audio_link, format_transcript


class NotFound(Exception): pass
class Conflict(Exception): pass


def now(): return datetime.now(timezone.utc).isoformat()


def day_key(value: str) -> str:
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return value


def filename(value: str) -> str:
    value = value.replace("/", "_").replace("\\", "_").replace("\x00", "_").strip()
    if not value or value in (".", "..") or len(value.encode()) > 230:
        raise ValueError("invalid filename")
    return value


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o660)
            out.write(content); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
        d = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(d)
        finally: os.close(d)
    finally:
        Path(name).unlink(missing_ok=True)


class Archive:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        for rel in ("archives/audio", "raw", "locks"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        self.database = self.root / "archive.sqlite"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS days(date TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audio(
              id TEXT PRIMARY KEY, date TEXT NOT NULL REFERENCES days(date) ON DELETE CASCADE,
              source_id TEXT UNIQUE, name TEXT NOT NULL, start_at TEXT, duration_ms INTEGER,
              UNIQUE(date,name));
            CREATE INDEX IF NOT EXISTS audio_date ON audio(date,start_at,id);
            CREATE TABLE IF NOT EXISTS jobs(
              date TEXT PRIMARY KEY, state TEXT NOT NULL, stage TEXT, error TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS steps(
              date TEXT NOT NULL, key TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL,
              retry_at REAL NOT NULL DEFAULT 0, error TEXT, path TEXT, updated_at TEXT NOT NULL,
              PRIMARY KEY(date,key));
            """)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db: yield db
        finally: db.close()

    @contextlib.contextmanager
    def lock(self, day: str):
        day_key(day)
        with (self.root / "locks" / (day + ".lock")).open("a") as stream:
            try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise Conflict("archive is being processed") from None
            try: yield
            finally: fcntl.flock(stream, fcntl.LOCK_UN)

    def audio_directory(self, day): return self.root / "archives" / "audio" / day_key(day)
    def transcript_path(self, day): return self.root / "archives" / (day_key(day) + ".md")

    def ensure_day(self, day):
        day_key(day)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO days VALUES(?,?,?)", (day, now(), now()))

    def list_days(self, start_date=None, end_date=None, offset=0, limit=50, audio_only=False):
        if offset < 0 or not 1 <= limit <= 200: raise ValueError("invalid pagination")
        where, args = [], []
        for value, op in ((start_date, ">="), (end_date, "<=")):
            if value: where.append(f"d.date{op}?"); args.append(day_key(value))
        if audio_only:
            where.append("EXISTS (SELECT 1 FROM audio a WHERE a.date=d.date)")
        if start_date and end_date and start_date > end_date: raise ValueError("start date exceeds end date")
        suffix = " WHERE " + " AND ".join(where) if where else ""
        with self.connect() as db:
            total = db.execute("SELECT count(*) FROM days d" + suffix, args).fetchone()[0]
            rows = db.execute("SELECT d.*,(SELECT count(*) FROM audio a WHERE a.date=d.date) audio_count "
                              "FROM days d" + suffix + " ORDER BY d.date DESC LIMIT ? OFFSET ?",
                              [*args, limit, offset]).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "offset": offset, "limit": limit}

    def get_day(self, day, metadata_only=False):
        day_key(day)
        with self.connect() as db:
            row = db.execute("SELECT * FROM days WHERE date=?", (day,)).fetchone()
            if not row: raise NotFound("archive not found")
            audio = [dict(r) for r in db.execute("SELECT * FROM audio WHERE date=? ORDER BY start_at,id", (day,))]
        p = self.transcript_path(day)
        if metadata_only:
            return {**dict(row), "has_transcript": p.is_file(), "audio": audio}
        return {**dict(row), "markdown": p.read_text() if p.is_file() else None, "audio": audio}

    def create_day(self, day, markdown):
        with self.lock(day), self.connect() as db:
            if db.execute("SELECT 1 FROM days WHERE date=?", (day,)).fetchone(): raise Conflict("archive already exists")
            atomic_write(self.transcript_path(day), format_transcript(markdown, day, []).encode())
            db.execute("INSERT INTO days VALUES(?,?,?)", (day, now(), now()))
            db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?)", (day, "complete", None, None, now()))
        return self.get_day(day)

    def update_day(self, day, markdown):
        with self.lock(day):
            self.get_day(day)
            self.publish(day, markdown)
        return self.get_day(day)

    def publish(self, day, markdown):
        # Caller holds the day lock; readers see the previous file or the complete new file.
        with self.connect() as db:
            audio = [dict(r) for r in db.execute("SELECT * FROM audio WHERE date=? ORDER BY start_at,id", (day,))]
            atomic_write(self.transcript_path(day), format_transcript(markdown, day, audio).encode())
            db.execute("UPDATE days SET updated_at=? WHERE date=?", (now(), day))
        self.job(day, "complete")

    def delete_day(self, day):
        with self.lock(day), self.connect() as db:
            self.get_day(day)
            trash = self.root / (".deleted-" + uuid.uuid4().hex)
            trash.mkdir()
            paths = ((self.transcript_path(day), trash / "transcript.md"), (self.audio_directory(day), trash / "audio"))
            try:
                for source, destination in paths:
                    if source.exists(): os.replace(source, destination)
                db.execute("DELETE FROM days WHERE date=?", (day,))
                db.execute("DELETE FROM steps WHERE date=?", (day,))
                # Persist deletion suppression with the index, even if trash cleanup fails.
                db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?)", (day, "complete", None, None, now()))
                db.commit()
            except BaseException:
                for source, destination in paths:
                    if destination.exists(): os.replace(destination, source)
                trash.rmdir()
                raise
            if trash.exists(): shutil.rmtree(trash)
            raw = self.root / "raw" / day
            if raw.exists(): shutil.rmtree(raw)
        return {"deleted": day}

    def add_audio(self, day, name, source: Path, *, source_id=None, start_at=None, duration_ms=None):
        name = filename(name)
        with self.connect() as db:
            if source_id:
                old = db.execute("SELECT * FROM audio WHERE source_id=?", (source_id,)).fetchone()
                if old: return dict(old)
            if not db.execute("SELECT 1 FROM days WHERE date=?", (day,)).fetchone(): raise NotFound("archive not found")
            identifier = uuid.uuid4().hex
            if db.execute("SELECT 1 FROM audio WHERE date=? AND name=?", (day, name)).fetchone():
                p = Path(name); name = f"{p.stem}-{identifier[:8]}{p.suffix}"
            target = self.audio_directory(day) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            md = self.transcript_path(day)
            old_markdown, changed = md.read_text() if md.is_file() else None, False
            fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
                    shutil.copyfileobj(inp, out, 1024 * 1024); out.flush(); os.fsync(out.fileno()); os.fchmod(out.fileno(), 0o660)
                os.replace(tmp, target)
                db.execute("INSERT INTO audio VALUES(?,?,?,?,?,?)", (identifier, day, source_id, name, start_at, duration_ms))
                if old_markdown is not None:
                    audio = [dict(r) for r in db.execute("SELECT * FROM audio WHERE date=? ORDER BY start_at,id", (day,))]
                    markdown = format_transcript(old_markdown, day, audio)
                    if markdown != old_markdown:
                        atomic_write(md, markdown.encode()); changed = True
                db.execute("UPDATE days SET updated_at=? WHERE date=?", (now(), day))
                db.commit()
            except BaseException:
                Path(tmp).unlink(missing_ok=True); target.unlink(missing_ok=True)
                if changed: atomic_write(md, old_markdown.encode())
                raise
        return self.get_audio(day, identifier)

    def get_audio(self, day, identifier):
        day_key(day)
        with self.connect() as db:
            row = db.execute("SELECT * FROM audio WHERE date=? AND id=?", (day, identifier)).fetchone()
        if not row: raise NotFound("audio not found")
        return dict(row)

    def audio_path(self, day, identifier):
        row = self.get_audio(day, identifier)
        return self.audio_directory(day) / row["name"]

    def get_audio_by_name(self, day, name):
        with self.connect() as db:
            row = db.execute("SELECT * FROM audio WHERE date=? AND name=?", (day_key(day), name)).fetchone()
        if not row: raise NotFound("audio not found")
        return dict(row)

    def read_audio(self, day, identifier, offset_bytes=0, limit_bytes=262144):
        import base64
        if offset_bytes < 0 or not 1 <= limit_bytes <= 262144: raise ValueError("invalid audio page")
        path = self.audio_path(day, identifier)
        with path.open("rb") as stream:
            stream.seek(offset_bytes); data = stream.read(limit_bytes)
        size = path.stat().st_size
        end = offset_bytes + len(data)
        return {"id": identifier, "name": path.name, "size": size, "offset_bytes": offset_bytes,
                "base64": base64.b64encode(data).decode(), "next_offset": end if end < size else None}

    def update_audio(self, day, identifier, name):
        name = filename(name)
        with self.lock(day), self.connect() as db:
            row = self.get_audio(day, identifier)
            if row["name"] == name: return row
            if db.execute("SELECT 1 FROM audio WHERE date=? AND name=?", (day, name)).fetchone(): raise Conflict("filename already exists")
            old = self.audio_path(day, identifier); new = old.with_name(name)
            md = self.transcript_path(day)
            old_markdown = md.read_text() if md.is_file() else None
            markdown = change_audio_link(old_markdown, day, row["name"], name) if old_markdown is not None else None
            changed = False
            os.replace(old, new)
            try:
                if markdown != old_markdown: atomic_write(md, markdown.encode()); changed = True
                db.execute("UPDATE audio SET name=? WHERE id=?", (name, identifier))
                db.execute("UPDATE days SET updated_at=? WHERE date=?", (now(), day)); db.commit()
            except BaseException:
                os.replace(new, old)
                if changed: atomic_write(md, old_markdown.encode())
                raise
        return self.get_audio(day, identifier)

    def delete_audio(self, day, identifier):
        with self.lock(day), self.connect() as db:
            row = self.get_audio(day, identifier)
            p = self.audio_directory(day) / row["name"]
            trash = p.with_name(".deleted-" + uuid.uuid4().hex)
            md = self.transcript_path(day)
            old_markdown = md.read_text() if md.is_file() else None
            markdown = change_audio_link(old_markdown, day, row["name"]) if old_markdown is not None else None
            changed = False
            os.replace(p, trash)
            try:
                if markdown != old_markdown: atomic_write(md, markdown.encode()); changed = True
                db.execute("DELETE FROM audio WHERE id=?", (identifier,))
                db.execute("UPDATE days SET updated_at=? WHERE date=?", (now(), day)); db.commit()
            except BaseException:
                os.replace(trash, p)
                if changed: atomic_write(md, old_markdown.encode())
                raise
            trash.unlink()
        return {"deleted": identifier}

    def job(self, day, state, stage=None, error=None):
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
                       "state=excluded.state,stage=excluded.stage,error=excluded.error,updated_at=excluded.updated_at",
                       (day_key(day), state, stage, error, now()))

    def status(self, day=None, limit=50):
        if not 1 <= limit <= 200: raise ValueError("invalid limit")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs" + (" WHERE date=?" if day else "") + " ORDER BY date DESC LIMIT ?",
                              (day_key(day), limit) if day else (limit,)).fetchall()
            output = []
            for row in rows:
                steps = [dict(r) for r in db.execute("SELECT key,state,attempts,retry_at,error FROM steps WHERE date=? ORDER BY key", (row["date"],))]
                output.append({**dict(row), "steps": steps})
        return output

    def step(self, day, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM steps WHERE date=? AND key=?", (day, key)).fetchone()
        return dict(row) if row else None

    def save_step(self, day, key, state, attempts, *, retry_at=0, error=None, path=None):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO steps VALUES(?,?,?,?,?,?,?,?)", (day, key, state, attempts, retry_at, error, str(path) if path else None, now()))

    def cleanup_raw(self, retention_days=30, clock=None):
        import time
        cutoff = (time.time() if clock is None else clock) - retention_days * 86400
        removed = 0
        for directory in (self.root / "raw").iterdir():
            if not directory.is_dir(): continue
            try:
                with self.lock(directory.name):
                    for path in directory.rglob("*"):
                        if path.is_file() and path.stat().st_mtime < cutoff:
                            path.unlink(); removed += 1
                    for path in sorted(directory.rglob("*"), reverse=True):
                        if path.is_dir() and not any(path.iterdir()): path.rmdir()
                    if not any(directory.iterdir()): directory.rmdir()
            except Conflict: continue
        return {"removed_files": removed}

    def backup(self, destination):
        target = Path(destination)
        if target.exists(): raise Conflict("backup destination exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(target) as output: source.backup(output)
