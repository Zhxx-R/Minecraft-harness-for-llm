# Minecraft Agent Harness：Windows 完整迁移包

这份迁移包用于把当前项目的前端、后端、Mineflayer worker、PostgreSQL/pgvector 数据、Redis 运行依赖、历史 `runs`、SQLite 审计库、Minecraft 世界和 MineCLIP 模型迁移到一台 Windows 10/11 电脑。

## 1. 推荐安装目录

把压缩包放到 Windows 本机磁盘，解压到一个短路径，例如：

```text
C:\mc-agent-harness
```

不要直接在压缩包内运行，也不推荐放在 OneDrive、中文长路径或网络盘中。解压后的项目根目录应直接看到 `backend`、`frontend`、`workers`、`windows` 和 `docker-compose.yml`。

## 2. 系统要求

- Windows 10 22H2 或 Windows 11 64 位。
- Docker Desktop，启用 WSL2 backend。
- Python 3.11 x64。安装时启用 `py` launcher 或加入 PATH。
- Node.js 22 LTS（最低满足 Vite 要求的 20.19）。
- Java 17 x64。
- 建议 16GB 内存、至少 15GB 可用磁盘空间。
- PowerShell 5.1 或 PowerShell 7。

所有服务只绑定 `127.0.0.1`。不要在未做安全评估时修改成公网监听。

## 3. 交给另一台电脑上的 Codex

在 Codex 中打开解压后的项目根目录，把 `windows/PROMPT_FOR_CODEX_WINDOWS.zh-CN.txt` 的内容原样发给它。Codex 应按以下顺序执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\windows\check-prerequisites.ps1
.\windows\setup.ps1 -RestoreDatabase -AllowDatabaseReset
.\windows\start-all.ps1
.\windows\verify.ps1
```

`-AllowDatabaseReset` 会删除并重建这份项目 Docker Compose 中的 `mc_agent` 数据库，只能用于确认可覆盖的目标库。它不会删除压缩包内的备份。

## 4. 首次配置做了什么

`setup.ps1` 会：

1. 从 `windows/.env.windows.example` 生成新的 `.env`。
2. 为 PostgreSQL 和 Minecraft RCON 生成新的随机本地密码。
3. 创建 Windows 专用 Python 虚拟环境。
4. 使用锁定文件安装后端、前端和 worker 依赖。
5. 启动 PostgreSQL/pgvector 与 Redis。
6. 从 `database/postgres/mc_agent.dump` 恢复主数据库。
7. 执行 Alembic 最新迁移并运行基础验证。

默认验证包括 Schema、Python 编译、worker 类型检查和前端类型检查。需要运行完整后端 pytest 时追加 `-FullTests`。打包源在本机验证时为 `316 passed / 6 failed`：其中 4 项因受限打包环境不允许测试进程绑定 `127.0.0.1`，另 2 项是现有 scripted benchmark/training 成功数断言回归；它们不由 Windows 迁移脚本引入，但接收端 Codex 应在后续开发中继续确认。

真实 API Key 不在迁移包中。需要模型能力时，在 Windows 本机编辑 `.env`：

```dotenv
QWEN_API_KEY=<你的本机 API Key>
```

不要把 Key 粘贴到 Codex 对话、日志或 Git。

## 5. 启动与停止

启动前端、后端、worker 和 Docker 基础设施：

```powershell
.\windows\start-all.ps1
```

打开：

- 前端：<http://127.0.0.1:5173>
- 后端健康检查：<http://127.0.0.1:8000/api/health>

停止项目进程：

```powershell
.\windows\stop-all.ps1
```

同时停止 PostgreSQL/Redis 容器：

```powershell
.\windows\stop-all.ps1 -StopInfrastructure
```

日志和 PID 文件位于 `.runtime\windows\`。

## 6. Minecraft Server

迁移包带有当前 Minecraft 1.20.1 server 文件和世界数据，但不会携带 `eula.txt` 或旧 RCON 密码。接收者必须亲自阅读 <https://aka.ms/MinecraftEULA> 并明确接受后，再运行：

```powershell
.\windows\start-minecraft.ps1 -AcceptEula
```

停止 Minecraft：

```powershell
.\windows\stop-all.ps1
```

`-AcceptEula` 会在这台 Windows 电脑上记录本人的接受状态；不能由打包方代替。

## 7. 可选 MineCLIP

权重和官方代码已包含，但 PyTorch 环境必须在 Windows 重新安装：

```powershell
.\windows\setup-mineclip.ps1
.\windows\start-all.ps1 -WithMineclip
```

默认 PyPI 安装在没有匹配 CUDA 环境时可能使用 CPU，评估会较慢。核心 dashboard、programmatic 任务和数据库浏览不依赖 MineCLIP。

## 8. 数据范围

包含：

- 当前磁盘上的工作树，包括未提交与未跟踪代码。
- `runs/` 下的 JSON、日志、图片、录屏和 SQLite 审计库。
- `week10-recovery-20260719/` 中的恢复备份。
- Minecraft 单服/服务池世界与二进制。
- MineCLIP `attn.pth` 权重、vendor 代码和缓存。
- PostgreSQL custom-format 逻辑备份。

不包含：

- `.git`、macOS Python 虚拟环境、`node_modules`、编译缓存。
- `.env`、`.env.local`、API Key、旧数据库密码、旧 RCON 密码。
- Minecraft `eula.txt`、`server.properties` 和运行中的 PID/lock。

本包使用项目中可用的最新 PostgreSQL 逻辑备份，源时间为 `2026-07-28 16:19:52 +08:00`。当时备份名为 `mc_agent_20260728_pre_trace_spans.dump`；恢复后脚本会执行最新 Alembic 迁移。该时间之后生成的文件型运行产物仍保存在 `runs/`，但若某次运行只写入了当时在线的 PostgreSQL 且没有 SQLite 副本，它不会出现在这份较早的主库快照中。原因是打包时原数据库引擎已不在本机，无法生成更晚的逻辑导出。

## 9. Windows 已知限制

- `backend/src/mc_agent_harness/runtime/macos_window_capture.py` 是 macOS 专用。Windows 可以运行核心 agent、Minecraft、dashboard 和数据库，但 Week 11 的本机游戏窗口录屏需另接 OBS/FFmpeg 或实现 Windows capture adapter。
- 历史 JSON/数据库记录中保留了原 macOS 的绝对路径，作为来源证据。实际文件已按原相对目录打包；新运行会使用 Windows 当前目录。
- 不要复制 macOS 的 PostgreSQL volume、`.venv` 或 `node_modules`，这些内容在 Windows 不兼容。

## 10. 完整性校验

压缩包旁的 `.sha256` 用于校验整个 ZIP。解压后可进行完整文件校验：

```powershell
.\windows\verify.ps1 -FullFileHash
```

该操作会读取数 GB 数据，可能需要数分钟。

如果 ZIP 大于 4GB，打包目录还会提供 `.part001`、`.part002` 等 FAT32 兼容分卷。把所有分卷、各自的 `.sha256` 和 `REASSEMBLE.ps1` 放在同一目录，再运行：

```powershell
.\REASSEMBLE.ps1 -BaseArchiveName .\minecraft-agent-harness-windows-complete-<timestamp>.zip
```

重组后仍要用整个 ZIP 的 `.sha256` 再校验一次。
