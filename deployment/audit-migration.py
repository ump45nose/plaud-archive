"""One-time inventory/content audit. Hashes are not part of runtime state."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

from plaud_archive.audio import probe


def rows(database, table):
    # Inputs are frozen migration/backup snapshots: never create WAL/SHM beside originals.
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try: return [dict(r) for r in connection.execute("SELECT * FROM " + table)]
    finally: connection.close()


def digest(path):
    with path.open("rb") as stream: return hashlib.file_digest(stream, "sha256").hexdigest()


def opus_payload_digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        page = 0
        while header := stream.read(27):
            if len(header) != 27 or header[:4] != b"OggS": raise ValueError("invalid Ogg page")
            laces = stream.read(header[26]); body = stream.read(sum(laces))
            if len(body) != sum(laces): raise ValueError("incomplete Ogg page")
            if page >= 2: result.update(body)
            page += 1
    return result.hexdigest()


parser = argparse.ArgumentParser()
parser.add_argument("--source-root", action="append", required=True)
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--snapshot", type=Path, required=True, help="SQLite backup created as the service user")
parser.add_argument("--report", type=Path, required=True)
args = parser.parse_args()
legacy = {}
for root in args.source_root:
    for row in rows(Path(root) / "state/state.db", "recordings"):
        legacy.setdefault(row["recording_id"], []).append(row)
current = rows(args.snapshot, "audio")
by_source = {r["source_id"]: r for r in current}
missing = sorted(set(legacy) - set(by_source))
unexpected = sorted(set(by_source) - set(legacy))


def audit(source_id):
    target = by_source[source_id]
    path = args.root / "archives" / target["date"] / "audio" / target["name"]
    checked = []
    for old in legacy[source_id]:
        original = Path(old["local_path"] or "")
        if not original.is_file(): continue
        original_hash = digest(original)
        if old.get("sha256") and original_hash != old["sha256"]: raise ValueError("legacy registered hash mismatch: " + source_id)
        try: probe(original); native = True
        except ValueError: native = False
        if native:
            if digest(path) != original_hash: raise ValueError("copied audio differs: " + source_id)
        elif opus_payload_digest(path) != original_hash:
            raise ValueError("wrapped Opus packets differ: " + source_id)
        checked.append("identical bytes" if native else "identical Opus payload")
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-threads", "1", "-i", str(path), "-f", "null", "-"], capture_output=True)
    if decoded.returncode: raise ValueError("full decode failed: " + source_id + " " + decoded.stderr.decode(errors="replace")[-200:])
    duration = probe(path)[1]
    return {"source_id": source_id, "date": target["date"], "name": target["name"], "old_copies_checked": checked,
            "recollected": not checked, "bytes": path.stat().st_size, "duration_seconds": duration,
            "registered_duration_seconds": target["duration_ms"] / 1000, "full_decode": True}


with ThreadPoolExecutor(max_workers=2) as pool:
    audited = list(pool.map(audit, sorted(set(legacy) & set(by_source))))
report = {"old_inventory_count": len(legacy), "new_audio_count": len(current), "missing": missing, "unexpected": unexpected,
          "total_hours": sum(a["duration_seconds"] for a in audited) / 3600, "recordings": audited}
args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({k: v for k, v in report.items() if k != "recordings"}))
raise SystemExit(2 if missing or unexpected else 0)
