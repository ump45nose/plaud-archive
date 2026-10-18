from __future__ import annotations

from mcp_platform import authorize, create_server


def create_mcp(archive, runtime):
    server = create_server(runtime, instructions="管理每日 Plaud 归档及其音频。转录正文是可直接阅读的 Markdown；处理状态单独查询。")
    read, write = authorize("read", runtime), authorize("write", runtime)

    @server.tool(auth=read)
    def list_archives(start_date: str | None = None, end_date: str | None = None, offset: int = 0, limit: int = 50) -> dict:
        """按日期分页列出归档概览。"""
        return archive.list_days(start_date, end_date, offset, limit)

    @server.tool(auth=read)
    def get_archive(date: str) -> dict:
        """读取一天的 Markdown 和音频列表。"""
        return archive.get_day(date)

    @server.tool(auth=read)
    def get_transcript(date: str) -> str:
        """直接返回当天干净的 Markdown 正文。"""
        from .store import NotFound
        value = archive.get_day(date)["markdown"]
        if value is None: raise NotFound("transcript not published")
        return value

    @server.tool(auth=write)
    def create_archive(date: str, markdown: str) -> dict:
        """创建每日归档，日期已存在时报冲突。"""
        return archive.create_day(date, markdown)

    @server.tool(auth=write)
    def update_archive(date: str, markdown: str) -> dict:
        """替换 Markdown 正文，不触发转录。"""
        return archive.update_day(date, markdown)

    @server.tool(auth=write)
    def delete_archive(date: str) -> dict:
        """删除当天 Markdown 和音频，不会自动重新采集。"""
        return archive.delete_day(date)

    @server.tool(auth=read)
    def get_audio(date: str, id: str) -> dict:
        """读取音频信息和下载路径；文件通过同一服务的受保护 HTTP 路径读取。"""
        row = archive.get_audio(date, id)
        return {**row, "download_path": f"/api/v1/archives/{date}/audio/{id}"}

    @server.tool(auth=write)
    def create_audio(date: str, name: str, content_base64: str) -> dict:
        """上传不超过 8 MiB 的音频；大文件使用 HTTP multipart 上传。"""
        import base64
        import os
        import tempfile
        from pathlib import Path
        if len(content_base64) > 12 * 1024 * 1024: raise ValueError("use HTTP upload for audio larger than 8 MiB")
        data = base64.b64decode(content_base64, validate=True)
        if len(data) > 8 * 1024 * 1024: raise ValueError("use HTTP upload for audio larger than 8 MiB")
        fd, name_on_disk = tempfile.mkstemp(dir=archive.root)
        try:
            with os.fdopen(fd, "wb") as out: out.write(data)
            with archive.lock(date): return archive.add_audio(date, name, Path(name_on_disk))
        finally: Path(name_on_disk).unlink(missing_ok=True)

    @server.tool(auth=write)
    def update_audio(date: str, id: str, name: str) -> dict:
        """修改归档音频文件名。"""
        return archive.update_audio(date, id, name)

    @server.tool(auth=write)
    def delete_audio(date: str, id: str) -> dict:
        """删除指定归档音频。"""
        return archive.delete_audio(date, id)

    @server.tool(auth=read)
    def get_processing_status(date: str | None = None, limit: int = 50) -> list[dict]:
        """读取处理阶段、尝试次数和错误。"""
        return archive.status(date, limit)

    return server
