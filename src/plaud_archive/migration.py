from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from .audio import playable
from .pipeline import ENGINES
from .store import Conflict, atomic_write, filename, now
from .transcript import format_transcript, legacy_speaker_labels


def migrate_layout(archive, keep_labels=()):
    """Explicit, resumable conversion of the former per-day archive directories."""
    with archive.connect() as db: days = [r[0] for r in db.execute("SELECT date FROM days ORDER BY date")]
    report = {"audio_moved": 0, "markdown_moved": 0, "dates": []}
    for day in days:
        with archive.lock(day):
            old = archive.root / "archives" / day
            if not old.is_dir(): continue
            old_audio, audio_directory = old / "audio", archive.audio_directory(day)
            if old_audio.is_dir():
                if audio_directory.exists(): raise Conflict("both audio layouts exist: " + day)
                audio_directory.parent.mkdir(parents=True, exist_ok=True)
                count = len(list(old_audio.iterdir()))
                os.replace(old_audio, audio_directory); report["audio_moved"] += count
            with archive.connect() as db:
                audio = [dict(r) for r in db.execute("SELECT * FROM audio WHERE date=? ORDER BY start_at,id", (day,))]
            for item in audio:
                if not (audio_directory / item["name"]).is_file(): raise FileNotFoundError("archive audio missing: " + item["name"])
            source, destination = old / "transcript.md", archive.transcript_path(day)
            if source.is_file():
                markdown = format_transcript(legacy_speaker_labels(source.read_text(), keep_labels), day, audio)
                if destination.exists() and destination.read_text() != markdown:
                    raise Conflict("both transcript layouts exist with different contents: " + day)
                if not destination.exists(): atomic_write(destination, markdown.encode())
                source.unlink(); report["markdown_moved"] += 1
                with archive.connect() as db: db.execute("UPDATE days SET updated_at=? WHERE date=?", (now(), day))
            if not any(old.iterdir()): old.rmdir()
            report["dates"].append(day)
    return report


def migrate(archive, config, roots, report_path):
    """One-time adapter: copy each source audio once and adopt matching completed RAW."""
    inventory, pending, conflicts = {}, [], []
    for root in map(Path, roots):
        with sqlite3.connect((root / "state/state.db").resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            for r in db.execute("SELECT * FROM recordings"):
                row = dict(r)
                if not row.get("local_path") or not Path(row["local_path"]).is_file():
                    pending.append({"source_id": row["recording_id"], "date": row["local_date"]}); continue
                old = inventory.get(row["recording_id"])
                if old and old["sha256"] != row["sha256"]: conflicts.append(row["recording_id"])
                if not old: inventory[row["recording_id"]] = row
    if conflicts: raise ValueError("conflicting source audio identities: " + ",".join(conflicts))
    report = {"recordings": [], "pending_downloads": pending, "raw_adopted": 0, "errors": []}
    by_source = {}
    for sid, row in inventory.items():
        day = row["local_date"]
        archive.ensure_day(day)
        with archive.lock(day):
            with archive.connect() as db: old = db.execute("SELECT * FROM audio WHERE source_id=?", (sid,)).fetchone()
            if old:
                by_source[sid] = dict(old); continue
            source = Path(row["local_path"])
            metadata = {}
            for root in map(Path, roots):
                p = root / "raw" / (sid + ".source-metadata.json")
                if p.is_file(): metadata = json.loads(p.read_text()); break
            try:
                wrapped = archive.root / "raw" / day / (".import-" + uuid.uuid4().hex + ".opus")
                path, suffix, duration = playable(source, raw_opus=row["media_format"] in ("opusraw", "media"), output=wrapped)
                name = filename(metadata.get("name") or row["start_at"].replace(":", "-"))
                if not name.lower().endswith(suffix): name += suffix
                audio = archive.add_audio(day, name, path, source_id=sid, start_at=row["start_at"], duration_ms=round(duration * 1000))
                # Wrapping legacy packet streams never mutates or replaces the old source.
                if path != source: path.unlink(missing_ok=True)
                by_source[sid] = audio
                report["recordings"].append({"source_id": sid, "date": day, "name": audio["name"], "bytes": archive.audio_path(day, audio["id"]).stat().st_size})
            except Exception as error:
                report["errors"].append({"source_id": sid, "error": str(error)[:300]})
        archive.job(day, "queued")
    source_roots = set()
    for root in map(Path, roots):
        for candidate in (root / "sources", root / "daily-v3/sources"):
            if candidate.is_dir(): source_roots.add(candidate)
    for root in source_roots:
        for sid, audio in by_source.items():
            if sid not in inventory: continue
            for sample_path in sorted((root / sid).glob("*/*/sample.json")):
                sample = json.loads(sample_path.read_text())
                if sample.get("source_sha256") != inventory[sid]["sha256"]: continue
                if sample["offset_seconds"] % config.audio_chunk_seconds: continue
                block = int(sample["offset_seconds"] // config.audio_chunk_seconds)
                folder = sample_path.parent
                destination = archive.root / "raw" / audio["date"] / audio["id"] / f"b{block:04d}"
                for engine in ENGINES:
                    key = f"{audio['id']}:{block}:{engine}"
                    if archive.step(audio["date"], key): continue
                    value = legacy_result(folder, engine)
                    if value is None or not value["text"].strip(): continue
                    target = destination / f"{engine}.json"
                    atomic_write(target, json.dumps(value, ensure_ascii=False).encode())
                    archive.save_step(audio["date"], key, "complete", 0, path=str(target.relative_to(archive.root)))
                    report["raw_adopted"] += 1
    for item in pending:
        if item["source_id"] not in by_source:
            archive.ensure_day(item["date"]); archive.job(item["date"], "queued")
    report["audio_count"] = len(by_source)
    report["source_count"] = len(inventory)
    atomic_write(Path(report_path), json.dumps(report, ensure_ascii=False, indent=2).encode())
    return report


def legacy_result(folder, engine):
    name = {"groq": "groq-whisper-large-v3", "gemini": "gemini", "sensevoice": "sensevoice"}[engine]
    if not (folder / (name + "-summary.json")).is_file(): return None
    if engine == "groq":
        p = folder / (name + ".json")
        if not p.is_file(): return None
        wire = json.loads(p.read_text())
        rows = [{"start": r["start"], "text": r["text"]} for r in wire.get("segments", [])]
        return {"text": wire.get("text") or "\n\n".join(r["text"] for r in rows), "paragraphs": rows}
    if engine == "sensevoice":
        p = folder / "sensevoice-fixed.jsonl"
        if not p.is_file(): return None
        rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
        return {"text": "\n\n".join(r["text"] for r in rows), "paragraphs": [{"start": r["start_seconds"], "text": r["text"]} for r in rows]}
    p = folder / "gemini-transcript.md"
    if not p.is_file(): return None
    summary = json.loads((folder / "gemini-summary.json").read_text())
    if summary.get("finish_reason") != "stop": return None
    import re
    rows = []
    for match in re.finditer(r"^\s*(?:[-*]\s*)?\[(\d+):(\d+)(?::(\d+))?(?:[.,]\d+)?\]\s*(.+)$", p.read_text(), re.M):
        start = int(match[1])*60 + int(match[2]) if not match[3] else int(match[1])*3600 + int(match[2])*60 + int(match[3])
        rows.append({"start": start, "text": match[4]})
    return {"text": p.read_text(), **({"paragraphs": rows} if rows else {})}
