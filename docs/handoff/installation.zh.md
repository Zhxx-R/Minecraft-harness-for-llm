# Minecraft Agent Harness 完整安装与 Demo 指南

## 1. 安装包范围

安装包包含当前工作树源码、Python/Node 锁定依赖、共享 JSON Schema、Minecraft 知识快照、MineDojo programmatic catalog、可执行 task manifest、初始 skill、数据库迁移、前后端代码、自动安装脚本和完整开发文档。

安装包不会包含：

- `.env`、Qwen API Key、RCON 密码。
- Git 私有仓库元数据。
- 本机 `.venv`、`node_modules`。
- Minecraft 官方 server jar、Fabric/Carpet 二进制、世界存档和玩家数据。
- 历史 run 数据库、日志、截图和录屏。

这些排除项要么属于敏感/本机状态，要么由带校验和的脚本从官方发布源重新下载。

## 2. 支持环境

推荐环境：

- macOS 14+ 或 Ubuntu 22.04+/WSL2。
- Python 3.11+。
- Node.js 20.19+ 或 22.12+，推荐最新 Node.js 22 LTS。
- Java 17+，推荐 Java 17。
- Docker Desktop/Engine，仅在运行 PostgreSQL、pgvector、Redis 和完整 dashboard 服务时需要。
- 至少 12GB 可用内存；运行一个 Minecraft server 推荐保留 3GB Java heap。

macOS 常用安装命令：

```bash
xcode-select --install
brew install python@3.11 node@22 openjdk@17
```

Ubuntu 常用安装命令：

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv openjdk-17-jre-headless make curl
```

Ubuntu 上的 Node.js 建议通过 `nvm install 22` 或 NodeSource 安装，发行版默认仓库版本可能过旧。

Docker 请使用官方 Docker Desktop 或 Docker Engine 安装方式。

## 3. 校验和解压

macOS：

```bash
shasum -a 256 minecraft-agent-harness-handoff-*.tar.gz
cat minecraft-agent-harness-handoff-*.tar.gz.sha256
mkdir -p "$HOME/minecraft-agent-harness"
tar -xzf minecraft-agent-harness-handoff-*.tar.gz \
  --strip-components=1 \
  -C "$HOME/minecraft-agent-harness"
cd "$HOME/minecraft-agent-harness"
```

Linux 将 `shasum -a 256` 换成 `sha256sum`。

也可以使用安装器：

```bash
bash install_handoff.sh \
  ./minecraft-agent-harness-handoff-<timestamp>.tar.gz \
  "$HOME/minecraft-agent-harness"
```

安装器拒绝覆盖非空目录。

## 4. 环境检查与依赖安装

只检查系统依赖：

```bash
./scripts/handoff/bootstrap.sh --check-only
```

安装 Python 和 Node 依赖并执行完整 CI：

```bash
./scripts/handoff/bootstrap.sh
```

同时启动 PostgreSQL/pgvector 与 Redis：

```bash
./scripts/handoff/bootstrap.sh --with-infra
```

第一次准备 Minecraft server 前，接收者必须阅读并明确接受 <https://aka.ms/MinecraftEULA>：

```bash
./scripts/handoff/bootstrap.sh \
  --with-minecraft \
  --accept-minecraft-eula
```

bootstrap 会创建权限为 `600` 的 `.env`，并自动生成本地 RCON 密码；不会生成或复制模型 API Key。

## 5. 配置模型

编辑 `.env`：

```dotenv
MODEL_DEFAULT=qwen3.7-plus
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_API_KEY=<接收者自己的 API Key>
```

也可以只在当前 shell 导出：

```bash
export QWEN_API_KEY='<接收者自己的 API Key>'
```

不要把真实 key 写进文档、Git commit 或 Codex 对话。

## 6. Offline Demo

Offline demo 不启动 Minecraft，也不调用 LLM：

```bash
./scripts/handoff/run_demo.sh offline
```

它会验证 schema、生成 `harvest_1_dirt` 的完整 prompt，并运行确定性 benchmark。结果保存在：

```text
runs/handoff_demo_<UTC timestamp>/
```

一条命令完成 bootstrap 和 offline demo：

```bash
./scripts/handoff/autorun.sh offline
```

## 7. Live LLM + Minecraft Demo

如果尚未准备服务端：

```bash
./scripts/handoff/setup_minecraft_server.sh --accept-eula
```

执行真实的 `harvest_1_dirt`：

```bash
./scripts/handoff/run_demo.sh live --task-id harvest_1_dirt
```

一条命令完成依赖、服务端和 live demo：

```bash
./scripts/handoff/autorun.sh live \
  --accept-minecraft-eula \
  --task-id harvest_1_dirt
```

如果有 Minecraft 1.20.1 客户端玩家在线，可跟随 bot：

```bash
./scripts/handoff/run_demo.sh live \
  --task-id harvest_1_dirt \
  --spectator-player <客户端玩家名>
```

Live 产物包括：

- `model_verification.json`：模型名称、usage 和结构化输出验证。
- `live_demo.json`：训练汇总、任务结果和录制/旁观元数据。
- `live_demo.sqlite3`：完整 run、step、trajectory、model call、knowledge 和 skill 审计。
- `live_demo.log`：终端日志。

## 8. 审计 Dashboard

使用 live demo 的 SQLite：

```bash
./scripts/handoff/start_dashboard.sh \
  --database-path runs/handoff_demo_<timestamp>/live_demo.sqlite3
```

浏览器打开：

- 前端：<http://127.0.0.1:5173>
- 后端健康检查：<http://127.0.0.1:8000/api/health>

使用 PostgreSQL 默认库时，不传 `--database-path`，并先执行：

```bash
docker compose up -d postgres redis
PYTHONPATH=backend/src backend/.venv/bin/python -m alembic upgrade head
```

## 9. 停止服务

只停止 dashboard：

```bash
./scripts/handoff/stop_services.sh
```

同时停止 Minecraft 与 Docker：

```bash
./scripts/handoff/stop_services.sh --minecraft --docker
```

## 10. 常见故障

### Docker API 无法连接

先启动 Docker Desktop，再运行 `docker info`。Live demo 默认使用 SQLite，不依赖 Docker。

### Java 版本错误

确认：

```bash
java -version
```

Minecraft 1.20.1 推荐 Java 17。macOS Homebrew 安装后可能需要设置 `JAVA_HOME`。

### 模型请求失败

运行：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/verify_llm_model.py
```

检查 `.env` 中的 `QWEN_BASE_URL`、`MODEL_DEFAULT` 与接收者自己的 key。

### Worker 无法连接 Minecraft

检查：

```bash
tail -f infra/minecraft-server/server.log
lsof -nP -iTCP:25565 -sTCP:LISTEN
```

服务端只监听 `127.0.0.1`。如需远程连接，必须先重新评估离线模式、RCON 暴露和防火墙风险。

### Demo 失败是否代表安装失败

不一定。LLM Minecraft task 可以因世界生成、模型决策或超时失败。安装验收看进程是否完整结束、审计记录是否落盘、失败原因是否可解释；任务成功率属于后续评测指标。

### npm audit 的 Mineflayer moderate 提示

当前 Mineflayer 最新版本的认证依赖链仍通过 `minecraft-protocol` 引入旧版 `uuid`。npm 提供的自动修复是将 Mineflayer 降级到不兼容的 1.4.0，因此安装脚本不会执行 `npm audit fix --force`。默认 demo 使用仅绑定 `127.0.0.1` 的 offline-mode server，不使用 Microsoft 在线认证链；在开放远程服务器或启用在线账号认证前必须重新审计该上游依赖。

## 11. 安装包完整性

解压后可以验证每个文件：

```bash
cd <项目根目录>
shasum -a 256 -c PACKAGE_MANIFEST.sha256
```

Linux 使用：

```bash
sha256sum -c PACKAGE_MANIFEST.sha256
```

`HANDOFF_BUILD_INFO.json` 记录打包时间、源 commit、工作树是否 dirty，以及安装包排除项。
