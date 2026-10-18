from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .settings import Workflow
from .store import Archive


def main():
    parser = argparse.ArgumentParser(description="Plaud collection, three-source transcription and archives")
    parser.add_argument("--config", default=os.getenv("PLAUD_CONFIG", "/home/yuwk/.config/plaud-archive/workflow.yaml"))
    parser.add_argument("--root", default=os.getenv("PLAUD_ARCHIVE_ROOT"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("run-due")
    commands.add_parser("cleanup")
    status = commands.add_parser("status"); status.add_argument("--date")
    for name in ("run-day", "retry"):
        cmd = commands.add_parser(name); cmd.add_argument("--date", required=True)
        if name == "run-day": cmd.add_argument("--skip-collect", action="store_true")
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--source-root", action="append", required=True)
    migrate.add_argument("--report", required=True)
    backup = commands.add_parser("backup"); backup.add_argument("--destination", required=True)
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        from mcp_platform import RuntimeConfig
        runtime = RuntimeConfig.from_env("plaud-archive")
        uvicorn.run(create_app(runtime=runtime), host=runtime.host, port=runtime.port, access_log=False)
        return
    config = Workflow.load(args.config) if Path(args.config).is_file() else None
    archive = Archive(args.root or (config.root if config else "/data"))
    if args.command == "status": result = archive.status(args.date)
    elif args.command == "backup": archive.backup(args.destination); result = {"database_snapshot": args.destination}
    else:
        if config is None: parser.error("workflow config required")
        if args.command == "cleanup": result = archive.cleanup_raw(config.raw_retention_days)
        elif args.command == "migrate":
            from .migration import migrate
            result = migrate(archive, config, args.source_root, args.report)
            result = {k: v for k, v in result.items() if k != "recordings"}
        else:
            from .pipeline import Pipeline
            pipeline = Pipeline(archive, config)
            if args.command == "retry": pipeline.retry(args.date); result = {"date": args.date, "state": "queued"}
            elif args.command == "run-day": result = pipeline.run_day(args.date, collect=not args.skip_collect)
            else: result = pipeline.run_due()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and result.get("state") == "failed": raise SystemExit(2)
    if isinstance(result, list) and any(r.get("state") == "failed" for r in result): raise SystemExit(2)


if __name__ == "__main__": main()
