# NAS 运行与一次性迁移验证

服务是 Hermes 988:984 的原生 systemd 进程，监听 LAN `192.168.31.201:18095`，通过已有 NPM TLS `:28443` 和 Authelia 对外提供 `/mcp`；不新建 Tunnel。配置源为 `mcp.app.yaml`，平台 target 的 `source_roots` 和 `env_files` 登记独立应用。`mcpctl plan` 当前会提出 Tunnel 变更，因此只协调本应用的既有认证与 NPM 路由，不直接执行该通用路由计划。

数据根 `/vol1/1000/plaud-archive`；私有工作流和服务 env 在 `/home/yuwk/.config/plaud-archive/`。保留原 Plaud MCP 登录和 Groq 密钥身份，CPA 使用现有实例。SenseVoice 模型复制至新根的 `models/sensevoice/`，运行不依赖旧数据目录。

数据库管理命令也必须以 Hermes 运行。FnOS 上其他用户即使只读 WAL 数据库，也可能创建归属错误的 `-wal`／`-shm`，影响写入。检查权限必须实际以服务身份打开、写入；不要依据 POSIX ACL 外观判断。

`rebuild-history.py` 是一次性、最多两天并行的历史补齐驱动，使用正式 Pipeline 的缓存、锁和有限重试。耗尽时退出，不替代 NAS 调度。历史重建不得以正在运行作为验收完成。

`audit-migration.py` 核对两套冻结库存的源 ID、原文件字节或封装前 Opus 载荷，并完整解码所有最终音频。先以服务身份做 SQLite 在线备份，再将 `--snapshot` 指向该快照；审计不会打开在用数据库或在旧库旁创建 WAL。哈希仅用于这次迁移核对，不保存为运行协议。

`verify-runtime.py` 通过真实公网 HTTPS 检查归档及音频 CRUD、Range、API 权限，并用同一生产文件检查 MCP 业务结果；完成后删除合成测试归档。它读取私有 env 后按数据目录身份执行文件写入。OAuth 发现和匿名拒绝单独验证；此脚本不代表用户已通过浏览器完成 OAuth 登录。

旧库、旧文件及本次认证/路由变更记录的一次性备份在 `/vol1/1000/personal-data-hub/source-backups/20261018-plaud-archive`。业务文件和 OAuth 状态应配套备份；数据库快照没有转录全文。
