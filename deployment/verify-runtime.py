"""One-time HTTPS integration check; credentials stay in the private env file."""
import argparse
import asyncio
import base64
import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin

import httpx
from fastmcp import Client
from mcp_platform import RuntimeConfig
from plaud_archive.mcp import create_mcp
from plaud_archive.store import Archive


parser = argparse.ArgumentParser()
parser.add_argument("--env", required=True)
parser.add_argument("--url", required=True)
parser.add_argument("--root", required=True)
parser.add_argument("--report", required=True)
args = parser.parse_args()
env = dict(line.split("=", 1) for line in Path(args.env).read_text().splitlines() if line and not line.startswith("#"))
# Exercise file writes as the actual service identity, including FnOS ACLs.
if os.geteuid() == 0:
    owner = Path(args.root).stat()
    os.setgroups([owner.st_gid]); os.setgid(owner.st_gid); os.setuid(owner.st_uid)
reader = {"Authorization": "Bearer " + env["PLAUD_API_READ_TOKEN"]}
writer = {"Authorization": "Bearer " + env["PLAUD_API_WRITE_TOKEN"]}
day = "1900-01-01"
path = "/api/v1/archives/" + day
archive = Archive(args.root)
report = {}
with httpx.Client(base_url=args.url, trust_env=False, timeout=60) as client:
    assert client.get("/healthz").status_code == 200
    assert client.get(path, headers=reader).status_code == 404, "verification date already exists"
    assert client.get("/api/v1/archives").status_code == 401
    body = {"date": day, "markdown": "集成检查"}
    assert client.post("/api/v1/archives", headers=reader, json=body).status_code == 403
    assert client.post("/api/v1/archives", headers=writer, json=body).status_code == 201
    try:
        text = f"# {day}\n\n## test\n\n**说话人 A：** 完整正文\n"
        assert client.patch(path, headers=writer, json={"markdown": text}).status_code == 200
        assert "**Speaker A:** 完整正文" in client.get(path + "/transcript.md", headers=reader).text
        upload = client.post(path + "/audio", headers=writer, files={"file": ("test.mp3", b"0123456789", "audio/mpeg")})
        assert upload.status_code == 201
        identifier = upload.json()["id"]
        audio_path = path + "/audio/" + identifier
        assert client.get(audio_path, headers=reader).content == b"0123456789"
        part = client.get(audio_path, headers={**reader, "Range": "bytes=2-4"})
        assert part.status_code == 206 and part.content == b"234"
        markdown = client.get(path + "/transcript.md", headers=reader)
        assert day + ".md" in markdown.headers["content-disposition"]
        href = re.search(r"\[音频\]\(([^)]+)\)", markdown.text)[1]
        linked = urljoin(str(markdown.url), href)
        assert client.get(linked).status_code == 401
        assert client.get(linked, headers=reader).content == b"0123456789"
        assert client.get(linked, headers={**reader, "Range": "bytes=2-4"}).content == b"234"
        assert client.patch(audio_path, headers=writer, json={"name": "renamed.mp3"}).json()["name"] == "renamed.mp3"
        assert client.get(linked, headers=reader).status_code == 404
        assert "renamed.mp3" in client.get(path + "/transcript.md", headers=reader).text
        async def compare():
            runtime = RuntimeConfig(name="plaud-archive", development_no_auth=True, host="127.0.0.1")
            async with Client(create_mcp(archive, runtime)) as mcp:
                result = await mcp.call_tool("get_transcript", {"date": day})
                assert result.content[0].text == client.get(path + "/transcript.md", headers=reader).text
                data = await mcp.call_tool("read_audio", {"date": day, "id": identifier})
                assert base64.b64decode(data.data["base64"]) == b"0123456789"
                changed = result.content[0].text.replace("完整正文", "MCP 修改正文")
                await mcp.call_tool("update_archive", {"date": day, "markdown": changed})
                assert client.get(path + "/transcript.md", headers=reader).text == changed
        asyncio.run(compare())
        assert client.delete(audio_path, headers=writer).status_code == 200
        assert client.get(audio_path, headers=reader).status_code == 404
        final = client.get(path + "/transcript.md", headers=reader).text
        assert "renamed.mp3" not in final and "MCP 修改正文" in final
        report.update(https_crud=True, audio_streaming_range=True, api_read_write_permissions=True, in_process_mcp_http_consistency=True,
                      portable_audio_links=True, audio_link_authentication=True, audio_links_follow_rename_delete=True)
    finally:
        assert client.delete(path, headers=writer).status_code == 200
    assert client.get(path, headers=reader).status_code == 404
    assert not archive.transcript_path(day).exists() and not archive.audio_directory(day).exists()
    with archive.connect() as db: db.execute("DELETE FROM jobs WHERE date=?", (day,))
    discovery = client.get("/.well-known/oauth-authorization-server")
    assert discovery.status_code == 200
    response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "verification", "version": "1"}}})
    assert response.status_code == 401
    report.update(oauth_discovery=True, anonymous_mcp_rejected=True, authenticated_https_oauth_session="not exercised")
Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
