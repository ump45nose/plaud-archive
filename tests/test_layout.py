import os
import re
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, urljoin

import pytest
from fastapi.testclient import TestClient
from mcp_platform import RuntimeConfig

from plaud_archive.api import create_app
from plaud_archive.migration import migrate_layout
from plaud_archive.store import Archive


DAY = "2026-10-17"
NAME = "2026-10-17 09:00:00 [版本](1)#?.opus"


def recording(tmp_path):
    archive = Archive(tmp_path / "data")
    markdown = f"# {DAY}\n\n## {Path(NAME).stem}\n\n**说话人 A：** 原始讨论内容。\n"
    archive.create_day(DAY, markdown)
    source = tmp_path / "input.opus"; source.write_bytes(b"0123456789")
    audio = archive.add_audio(DAY, NAME, source, source_id="remote-id", duration_ms=1000)
    return archive, audio


def test_flat_layout_and_audio_reference_lifecycle(tmp_path):
    archive, audio = recording(tmp_path)
    assert archive.transcript_path(DAY) == archive.root / "archives" / (DAY + ".md")
    assert archive.audio_path(DAY, audio["id"]) == archive.root / "archives/audio" / DAY / NAME
    body = archive.get_day(DAY)["markdown"]
    assert "**Speaker A:** 原始讨论内容。" in body
    old_link = "audio/" + DAY + "/" + quote(NAME, safe="")
    assert f"[音频]({old_link})" in body
    archive.update_audio(DAY, audio["id"], "renamed #.opus")
    body = archive.get_day(DAY)["markdown"]
    assert old_link not in body and "renamed%20%23.opus" in body
    # A renamed file remains associated with its original recording title.
    archive.update_day(DAY, body)
    assert archive.get_day(DAY)["markdown"] == body
    archive.delete_audio(DAY, audio["id"])
    assert "audio/" not in archive.get_day(DAY)["markdown"]
    assert "原始讨论内容。" in archive.get_day(DAY)["markdown"]
    assert archive.status(DAY)[0]["state"] == "complete"


def test_markdown_relative_audio_url_authentication_and_range(tmp_path):
    archive, audio = recording(tmp_path)
    runtime = RuntimeConfig(name="plaud-archive", development_no_auth=True, host="127.0.0.1")
    with TestClient(create_app(archive, runtime, "reader", "writer")) as client:
        headers = {"Authorization": "Bearer reader"}
        response = client.get(f"/api/v1/archives/{DAY}/transcript.md", headers=headers)
        assert DAY + ".md" in response.headers["content-disposition"]
        relative = re.search(r"\[音频\]\(([^)]+)\)", response.text)[1]
        url = urljoin(str(response.url), relative)
        assert client.get(url).status_code == 401
        assert client.get(url, headers=headers).content == b"0123456789"
        part = client.get(url, headers={**headers, "Range": "bytes=2-4"})
        assert part.status_code == 206 and part.content == b"234"
        assert client.get(url.replace("/audio/" + DAY, "/audio/2026-10-18"), headers=headers).status_code == 404
        archive.update_audio(DAY, audio["id"], "new.opus")
        assert client.get(url, headers=headers).status_code == 404
        assert client.get(f"/api/v1/archives/{DAY}/audio/{DAY}/new.opus", headers=headers).content == b"0123456789"


@pytest.mark.parametrize("operation", ["rename", "delete", "upload"])
def test_audio_mutation_rolls_back_if_markdown_write_fails(tmp_path, operation):
    archive, audio = recording(tmp_path)
    path = archive.audio_path(DAY, audio["id"])
    body = archive.get_day(DAY)["markdown"]
    if operation == "upload":
        archive.update_day(DAY, body + "\n## new\n\n新增录音正文。\n")
        body = archive.get_day(DAY)["markdown"]
        source = tmp_path / "new.mp3"; source.write_bytes(b"new")
    with patch("plaud_archive.store.atomic_write", side_effect=OSError("markdown write denied")):
        with pytest.raises(OSError, match="markdown write denied"):
            if operation == "rename": archive.update_audio(DAY, audio["id"], "new.opus")
            elif operation == "delete": archive.delete_audio(DAY, audio["id"])
            else: archive.add_audio(DAY, "new.mp3", source)
    assert archive.get_audio(DAY, audio["id"])["name"] == NAME
    assert path.read_bytes() == b"0123456789"
    assert archive.get_day(DAY)["markdown"] == body
    assert list(archive.audio_directory(DAY).iterdir()) == [path]
    assert len(archive.get_day(DAY)["audio"]) == 1


def test_day_delete_restores_both_paths_if_second_move_fails(tmp_path):
    archive, audio = recording(tmp_path)
    original = os.replace

    def fail_audio(source, destination):
        if Path(source) == archive.audio_directory(DAY): raise OSError("audio move denied")
        return original(source, destination)

    with patch("plaud_archive.store.os.replace", side_effect=fail_audio):
        with pytest.raises(OSError, match="audio move denied"): archive.delete_day(DAY)
    assert archive.transcript_path(DAY).is_file()
    assert archive.audio_path(DAY, audio["id"]).read_bytes() == b"0123456789"
    assert len(archive.get_day(DAY)["audio"]) == 1
    archive.delete_day(DAY)
    assert not archive.transcript_path(DAY).exists()
    assert not archive.audio_directory(DAY).exists()


def old_layout(tmp_path):
    archive, audio = recording(tmp_path)
    old = archive.root / "archives" / DAY; old.mkdir()
    os.replace(archive.audio_directory(DAY), old / "audio")
    os.replace(archive.transcript_path(DAY), old / "transcript.md")
    markdown = f"# {DAY}\n\n## {Path(NAME).stem}\n\n**说话人 A：** 第一段。\n\n"
    markdown += f"## 录音：{Path(NAME).stem}\n\n**Alice：** 第二段。\n\n**说明：** 保留此结构说明。\n"
    (old / "transcript.md").write_text(markdown)
    raw = archive.root / "raw" / DAY / "groq.json"; raw.parent.mkdir(); raw.write_text('{"text":"cached"}')
    archive.save_step(DAY, "groq", "complete", 1, path=str(raw.relative_to(archive.root)))
    return archive, audio, old


@pytest.mark.parametrize("interrupt", [False, True])
def test_layout_migration_moves_bytes_and_reuses_steps_after_interruption(tmp_path, interrupt):
    archive, audio, old = old_layout(tmp_path)
    inode = (old / "audio" / NAME).stat().st_ino
    step = archive.step(DAY, "groq")
    if interrupt:
        with patch("plaud_archive.migration.atomic_write", side_effect=OSError("write interrupted")):
            with pytest.raises(OSError, match="write interrupted"): migrate_layout(archive, ["说明"])
        assert (old / "transcript.md").is_file()
        assert archive.audio_path(DAY, audio["id"]).is_file()
    report = migrate_layout(archive, ["说明"])
    assert report["markdown_moved"] == 1
    assert archive.audio_path(DAY, audio["id"]).stat().st_ino == inode
    assert archive.audio_path(DAY, audio["id"]).read_bytes() == b"0123456789"
    assert archive.get_audio(DAY, audio["id"])["source_id"] == "remote-id"
    body = archive.get_day(DAY)["markdown"]
    assert "**Speaker A:** 第一段。" in body and "**Speaker B:** 第二段。" in body
    assert "**说明：** 保留此结构说明。" in body
    assert body.count("[音频](") == 1
    assert not old.exists()
    assert archive.step(DAY, "groq") == step
    assert migrate_layout(archive, ["说明"])["dates"] == []
    assert archive.get_day(DAY)["markdown"] == body
