"""One-time bounded migration driver. Runtime scheduling remains the NAS CLI job."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import sys
import time

from plaud_archive.pipeline import Pipeline
from plaud_archive.settings import Workflow
from plaud_archive.store import Archive, Conflict


config = Workflow.load(sys.argv[1])
archive = Archive(config.root)


def rebuild(day):
    try: return Pipeline(archive, config).run_day(day, collect=False)
    except Conflict: return {"date": day, "state": "busy"}


while True:
    with archive.connect() as db:
        rows = db.execute("SELECT date FROM jobs WHERE state!='complete' ORDER BY date").fetchall()
    due, waiting = [], []
    exhausted = []
    for row in rows:
        day = row["date"]
        state = archive.status(day)[0]
        steps = [s for s in state["steps"] if s["state"] != "complete"]
        if state["state"] == "failed" and not steps: exhausted.append(day); continue
        if any(s["attempts"] >= config.max_attempts for s in steps): exhausted.append(day); continue
        deadline = max((s["retry_at"] for s in steps), default=0)
        if deadline > time.time(): waiting.append(deadline)
        else: due.append(day)
    if due:
        busy = False
        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in as_completed([pool.submit(rebuild, day) for day in due]):
                result = future.result()
                print(json.dumps(result, ensure_ascii=False), flush=True)
                busy |= result["state"] == "busy"
        if busy: time.sleep(30)
    elif waiting:
        time.sleep(min(60, max(1, min(waiting) - time.time())))
    else:
        print(json.dumps({"state": "failed" if exhausted else "complete", "exhausted_dates": exhausted}), flush=True)
        raise SystemExit(2 if exhausted else 0)
