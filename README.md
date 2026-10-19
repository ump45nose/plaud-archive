# Plaud Archive

源码：[ump45nose/plaud-archive](https://github.com/ump45nose/plaud-archive)。

官方 MCP 采集 → Groq / Gemini / SenseVoice 三源转录 → 文件归档 ← HTTP / MCP。

每日一份可读 Markdown、当天各条可播放音频。正文不放来源引用、模型说明、逐句时间戳、候选或置信提示。SQLite 只存索引和任务结果，三源 RAW 与融合中间材料默认 30 天后清理。

## 安装与运行

Python 3.12+；采集需要 Node/npx 和原 Plaud MCP 登录，转录需要 ffmpeg/ffprobe。API 和定时 CLI 使用同一数据根目录和运行身份。

```sh
uv sync --extra server --extra local --extra test
.venv/bin/pytest -q
plaud-archive --config /private/workflow.yaml run-due
plaud-archive --config /private/workflow.yaml run-day --date 2026-10-17
plaud-archive --config /private/workflow.yaml retry --date 2026-10-17
plaud-archive --root /private/data status
plaud-archive serve
```

工作流配置见 `config.example.yaml`。每日 07:30 后创建昨天的任务，按开始日期归属；成功日期不再扫描云端，不处理迟到录音或自动更新。失败步骤至多尝试 3 次、间隔 30 分钟；手动 `retry` 仅重置未完成步骤。NAS 定时任务只是 CLI 包装，所有业务逻辑归本应用。

连续音频以 20 分钟处理切片覆盖尾段，三源完成后才融合。默认由 `gemini-3-flash` 转录音频，`gemini-3.8-flash-high`（CPA 的 3.8f）融合整理三源正文，两者可独立配置。融合素材按完整段落组包，输入默认上限 256000 tokens，使用固定 cl100k 分词器做本地计量，不推算模型总上下文或输出预算。单段超限会直接报错；可调整上限。保持整段和原始顺序，不引入检索或向量数据库。

目录为 `archive.sqlite`、`archives/YYYY-MM-DD.md`、`archives/audio/YYYY-MM-DD/原标题`、`raw/日期/`。同一录音只保留一个可播放音频，原 MP3 直接保存、裸 Opus 无损封装，格式不强制统一。每日 MD 使用 `Speaker A:`、`Speaker B:` 标签，每条录音标题下由业务层插入 `[音频](audio/日期/文件名)` 相对链接；文件名按 URL 编码，整个 `archives/` 目录可一起迁移。音频改名、删除同步修正引用，上传只给已有对应标题的段落补链接，不自动生成转录。RAW 保留策略不变，`cleanup` 仅清理到期 RAW。`backup --destination PATH` 使用 SQLite 在线快照；业务备份还须配套保存 `archives/` 和平台 OAuth 状态。

## HTTP 与 MCP

API 使用不同的 `PLAUD_API_READ_TOKEN` / `PLAUD_API_WRITE_TOKEN`，支持对应 `_FILE`；MCP 沿用平台 OAuth。生产缺少凭据拒绝启动，无认证开发仅允许回环。服务器设置 `PLAUD_ARCHIVE_ROOT`、公共运行时 OIDC/OAuth 环境变量。

MCP 授权用户通过 `OIDC_AUTHORIZED_USERNAMES` 配置，其中 `OIDC_READ_ONLY_USERNAMES` 的用户仅可读取；写入复用平台同一权限检查。Gemini 使用可配置模型名，示例为当前 CPA 已验证的别名；转录关闭思考并保留明确的完成/空响应/上游内容过滤错误，不将被阻断的响应当成成功。

- `/api/v1/archives`：GET 日期筛选和分页，POST `{date, markdown}` 创建。
- `/api/v1/archives/{date}`：GET 完整归档，PATCH `{markdown}` 替换正文，DELETE 删除正文和当天音频。
- `/api/v1/archives/{date}/transcript.md`：GET 干净 Markdown，下载文件名为 `{date}.md`。原接口路径保持。
- `/api/v1/archives/{date}/audio/{date}/{name}`：MD 内相对链接的文件读取入口，沿用相同读权限与 Range 支持。
- `/api/v1/archives/{date}/audio`：POST multipart 文件上传。
- `/api/v1/archives/{date}/audio/{id}`：GET 下载，PATCH `{name}` 改名，DELETE 删除。
- `/api/v1/status?date=YYYY-MM-DD`：处理阶段、尝试次数与错误；列表默认 50、最大 200。
- `/healthz`：进程/数据库检查；`/mcp`：同一业务层的 12 个读取与管理工具。MCP 小音频上传上限 8 MiB，大文件使用 HTTP 流式上传；音频支持受保护 HTTP 下载与同一 MCP 授权下的分页字节读取。

写操作遇到当天转录持锁返回 409。修改、删除不会触发转录或自动恢复。正文写入原子替换，未完成的融合不发布成品。

## 一次迁移

```sh
plaud-archive --config /private/workflow.yaml migrate \
  --source-root /private/old-download \
  --source-root /private/old-processing \
  --report /private/migration.json
```

迁移只读旧 SQLite 和文件，按来源 ID 合并库存、对冲突音频报错；裸 Opus 仅封装，不重编码。匹配原音频身份的已完成三源 RAW 可以复用。缺失下载显式进入报告，旧稿不作为新正式稿。旧备份不放在新运行目录。

已使用原 `archives/日期/transcript.md` 布局的实例，在备份索引、旧 MD 并停止本应用写入后，以相同服务身份执行 `plaud-archive --root /private/data migrate-layout`。此命令显式移动音频目录、转换旧说话人标签并写入相对链接，不触发转录；可中断续跑，同一路径存在不同内容时直接报冲突。历史 MD 中属于说明而非说话人的粗体标签，可用重复的 `--keep-label LABEL` 保留。

上线与进度见 [实施计划](docs/plans/2026-10-18-rebuild.md)。

NAS 的工作进程与服务都保留原 Hermes 身份（988:984）。外部 Groq、Plaud 下载复用现有出站代理，通过 `outbound_proxy` 配置；CPA 局域网请求直接连接。新数据目录只授予该应用需要的身份访问，旧凭据目录权限不扩大。
