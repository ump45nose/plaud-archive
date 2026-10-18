from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .audio import playable
from .store import filename


def payload(result):
    if result.isError: raise RuntimeError("Plaud MCP tool failed; check runtime login")
    text = "\n".join(getattr(p, "text", "") for p in result.content)
    if text.startswith("MCP error") or text.startswith("Error:"):
        raise RuntimeError(text[:300])
    # The explanatory preamble quotes the opening tag too. Start at the last actual opening tag.
    for m in reversed(list(re.finditer(r"<(untrusted-user-data-[^ >]+)(?:\s+[^>]*)?>", text))):
        end = text.find(f"</{m[1]}>", m.end())
        if end >= 0: return json.loads(text[m.end():end].strip())
    return json.loads(text)


class PlaudCollector:
    def __init__(self, config, archive): self.config, self.archive = config, archive

    async def collect(self, day):
        cfg, archive = self.config, self.archive
        env = dict(os.environ)
        if cfg.outbound_proxy:
            env.update(HTTP_PROXY=cfg.outbound_proxy, HTTPS_PROXY=cfg.outbound_proxy)
        if cfg.token_file:
            if not Path(cfg.token_file).is_file(): raise RuntimeError("Plaud login required")
            env["HOME"] = str(Path(cfg.token_file).parent.parent)
        params = StdioServerParameters(command=cfg.plaud_command, args=list(cfg.plaud_args), env=env)
        zone = ZoneInfo(cfg.timezone)
        cutoff = datetime.fromisoformat(day).replace(tzinfo=zone) - timedelta(days=1)
        count = 0
        with archive.connect() as db: known = {r[0] for r in db.execute("SELECT source_id FROM audio WHERE source_id IS NOT NULL")}
        async with stdio_client(params) as (inp, out):
            async with ClientSession(inp, out) as session:
                await session.initialize()
                for page in range(1, 101):
                    listing = payload(await session.call_tool("list_files", {"page": page, "page_size": 100}))["data"]
                    if not listing: return {"downloaded": count}
                    for item in listing:
                        if item["id"] in known: continue
                        detail = payload(await session.call_tool("get_file", {"file_id": item["id"]}))
                        start = datetime.fromisoformat(detail["start_at"].replace("Z", "+00:00"))
                        if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)
                        if start.astimezone(zone).date().isoformat() != day: continue
                        if not detail.get("presigned_url"): raise RuntimeError("Plaud audio download URL not ready")
                        self.download(day, detail); count += 1; known.add(item["id"])
                    dates = [datetime.fromisoformat(i["created_at"].replace("Z", "+00:00")) for i in listing]
                    dates = [d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d for d in dates]
                    if min(dates) < cutoff: return {"downloaded": count}
        raise RuntimeError("Plaud pagination did not finish")

    def download(self, day, detail):
        url = detail["presigned_url"]
        if not url.startswith("https://"): raise ValueError("Plaud returned an invalid audio URL")
        directory = self.archive.root / "raw" / day
        directory.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=directory, suffix=".download")
        source = Path(temp); normalized = source
        try:
            with os.fdopen(fd, "wb") as output, httpx.stream("GET", url, follow_redirects=True, timeout=120, proxy=self.config.outbound_proxy or None, trust_env=False) as response:
                response.raise_for_status()
                for block in response.iter_bytes(1024 * 1024): output.write(block)
                output.flush(); os.fsync(output.fileno())
                mime = response.headers.get("content-type", "")
            normalized, suffix, duration = playable(source, raw_opus=mime.startswith("audio/opus"))
            name = filename(detail.get("name") or detail["start_at"])
            if not name.lower().endswith(suffix): name = str(Path(name).with_suffix("")) + suffix
            self.archive.add_audio(day, name, normalized, source_id=detail["id"], start_at=detail["start_at"], duration_ms=round(duration * 1000))
        finally:
            source.unlink(missing_ok=True)
            if normalized != source: normalized.unlink(missing_ok=True)
