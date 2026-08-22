# Week 10 全量 5-worker 云服务器部署

## 推荐结论

这条训练链路不需要 GPU。推荐先租一台按量付费的 x86_64 Linux 云服务器：

- 16 vCPU、64 GiB 内存；希望缩短 Java tick 排队时可升到 24 vCPU、96 GiB。
- Ubuntu 24.04 LTS。
- 200 GiB SSD/ESSD 数据盘起步，开启云盘快照；五个世界会随随机传送持续增长。
- 公网只开放 SSH 22，并把来源限制为自己的公网 IP。不要开放 25565-25569、25575-25579、5432、6379、8000、8765 等训练端口。
- 第一轮使用按量付费，不建议使用会被回收的抢占/竞价实例。全量跑通并测得真实耗时后，再决定是否包月或使用抢占实例。

五个 worker 的实际拓扑是：

```text
worker-1 -> Minecraft server-1/world-1
worker-2 -> Minecraft server-2/world-2
worker-3 -> Minecraft server-3/world-3
worker-4 -> Minecraft server-4/world-4
worker-5 -> Minecraft server-5/world-5
                    |
                    +-> PostgreSQL skill/audit database
                    +-> remote Qwen API
```

当前 executable catalog 共 1581 条：895 harvest、471 combat、213 techtree、2 survival。旧的 formal 默认口径排除了 survival，因此只有 1579 条；服务器脚本显式使用 `--include-survival` 跑完整 1581 条。

## 1. 在本机生成无凭据迁移包

```bash
cd "/path/to/agent for minecraft"
backend/.venv/bin/python scripts/build_handoff_package.py --skip-zip
```

把 `release/` 下同一时间戳的三个文件传到服务器：

```text
minecraft-agent-harness-handoff-<timestamp>.tar.gz
minecraft-agent-harness-handoff-<timestamp>.tar.gz.sha256
install_handoff.sh
```

示例：

```bash
scp release/minecraft-agent-harness-handoff-<timestamp>.tar.gz* \
  release/install_handoff.sh ubuntu@SERVER_IP:~/
```

迁移包不包含 `.env`、API Key、旧数据库、Minecraft jar、世界和本机依赖；服务端会重新安装依赖并从校验过的官方地址下载 Minecraft/Fabric/Carpet。

## 2. 创建云服务器

建议购买参数：

```text
计费：按量付费
架构：x86_64/AMD64（不要选 ARM）
CPU/内存：16 vCPU / 64 GiB
镜像：Ubuntu 24.04 LTS
磁盘：200 GiB SSD/ESSD，可在线扩容并开启快照
公网：按流量计费即可，5-20 Mbps 足够安装与 SSH
安全组入站：只允许 你的公网IP/32 -> TCP 22
```

阿里云可直接选 `ecs.g8i.4xlarge`（16 vCPU / 64 GiB）；腾讯云选择同等级 1:4 通用型 CVM 即可。不要为本任务租 GPU 实例。

## 3. 安装 Ubuntu 系统依赖

SSH 登录后：

```bash
sudo apt update
sudo apt install -y python3 python3-venv openjdk-17-jre-headless \
  make build-essential curl ca-certificates tmux
```

安装 Node.js 22 LTS。可以使用 NodeSource 或 nvm；完成后确认：

```bash
node --version
npm --version
java -version
python3 --version
```

按 Docker 官方 Ubuntu 文档安装 Docker Engine 和 Compose plugin，然后允许当前用户运行 Docker，重新登录一次：

```bash
sudo usermod -aG docker "$USER"
```

## 4. 一次性 bootstrap

下列命令会校验压缩包、安装 Python/Node 依赖、启动 PostgreSQL/Redis，并在你明确接受 Minecraft EULA 后准备 1.20.1 Fabric + Carpet 服务端：

```bash
bash ~/install_handoff.sh \
  ~/minecraft-agent-harness-handoff-<timestamp>.tar.gz \
  ~/minecraft-agent-harness \
  --with-infra \
  --with-minecraft \
  --accept-minecraft-eula \
  --skip-tests
cd ~/minecraft-agent-harness
```

正式开跑前建议补做完整验收：

```bash
make ci
```

## 5. 只在服务器本地配置凭据

bootstrap 已在权限为 `600` 的 `.env` 中生成一致的 RCON/PostgreSQL 随机密码。只需编辑模型配置，并确认下面几项存在且密码互相匹配：

```dotenv
POSTGRES_PASSWORD=<bootstrap-generated-random-hex-password>
DATABASE_URL=postgresql+psycopg://mc_agent:<same-generated-password>@localhost:5432/mc_agent
MODEL_DEFAULT=qwen3.7-plus
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_API_KEY=<your-key>
MINECRAFT_RCON_PASSWORD=<bootstrap-generated-password>
```

不要通过聊天、Git 或迁移压缩包传 API Key。Compose 已只把 PostgreSQL/Redis 映射到 `127.0.0.1`，不要改成公网监听。若复用一个已经用旧密码初始化过的 PostgreSQL volume，单改 `.env` 不会修改数据库内部密码；此时应保留原密码，或先用 SQL 改库内密码再同步 `DATABASE_URL`。

先验证模型和 1581 条任务计划：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/verify_llm_model.py
./scripts/handoff/run_week10_full_5.sh plan
```

计划报告应显示 `task_count: 1581`、`worker_concurrency: 5`，类别计数应为 895/471/213/2。

## 6. 在 tmux 中启动全量训练

```bash
tmux new -s week10-full5
cd ~/minecraft-agent-harness
./scripts/handoff/run_week10_full_5.sh run
```

按 `Ctrl-b`，再按 `d`，即可退出 SSH 而不终止训练。重新查看：

```bash
tmux attach -t week10-full5
```

脚本会自动完成：

1. 启动 PostgreSQL 和 Redis。
2. 执行 Alembic 数据库迁移。
3. 启动 5 个隔离 Minecraft server，每个默认 3 GiB Java heap。
4. 启动 5 个 Mineflayer worker 和最多 5 路模型请求。
5. 运行全部 1581 条任务，每条初始执行一次，失败最多重试 5 次。
6. 用 best-effort diversity 把任务排成 317 个五任务 wave，让 5 个 worker 尽量保持占满。
7. 每个 wave 原子写 checkpoint；当前输出目录记录在 `.runtime/week10_full_5.latest`。

服务器入口默认设置 `WEEK10_MAX_TASK_SIMILARITY=1.0`，含义是仍优先挑选低相似任务搭配，但不因找不到低于阈值的任务而留下空闲 worker。如果你更重视同 wave 的 skill 差异、可以接受后半程利用率下降，可改回严格阈值：

```bash
WEEK10_MAX_TASK_SIMILARITY=0.45 \
  ./scripts/handoff/run_week10_full_5.sh run
```

当前 catalog 的严格计划会形成 624 个 wave；默认吞吐计划为 317 个 wave。

## 6A. 改为 24 小时预算训练

如果目标不是遍历全部 1581 条，而是要求在约 24 小时窗口内完成并产出一批 skill，使用独立入口：

```bash
./scripts/handoff/run_week10_24h_5.sh plan
tmux new -s week10-24h
cd ~/minecraft-agent-harness
./scripts/handoff/run_week10_24h_5.sh run
```

默认选择 100 条分层、多样化任务并排成 20 个满载 wave。预算依据是：

```text
100 tasks × 最多 6 attempts × 600 秒 ÷ 5 workers = 20 小时
```

剩余约 4 小时用于环境 reset、模型限流和进程开销。这个配置能约束项目自身的执行上限，但云服务故障、模型 API 长时间不可用等外部事件仍可能让总墙钟时间超过 24 小时。想再保守一些可改为 80 条：

```bash
WEEK10_TASK_COUNT=80 ./scripts/handoff/run_week10_24h_5.sh run
```

## 7. 中断后续跑

先读取原输出目录：

```bash
cd ~/minecraft-agent-harness
LATEST_DIR="$(cat .runtime/week10_full_5.latest)"
./scripts/handoff/run_week10_full_5.sh resume "$LATEST_DIR"
```

必须同时保留原 checkpoint、同一个 PostgreSQL 数据库和五个世界目录。不要只复制 checkpoint 后连接一个空数据库继续。

## 8. 备份与监控

至少每天创建一次云盘快照。手动备份时应同时保存 PostgreSQL、当前输出目录和五个世界：

```bash
cd ~/minecraft-agent-harness
LATEST_DIR="$(cat .runtime/week10_full_5.latest)"
BACKUP_DIR="backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"
docker compose exec -T postgres pg_dump -U mc_agent -d mc_agent -Fc \
  > "$BACKUP_DIR/mc_agent.dump"
tar -czf "$BACKUP_DIR/week10-state.tar.gz" \
  "$LATEST_DIR" infra/minecraft-server-pool
```

常用检查：

```bash
free -h
df -h
docker compose ps
ps -eo pid,pcpu,pmem,rss,cmd --sort=-rss | head -30
tail -f "$(cat .runtime/week10_full_5.latest)/week10_formal_batch.log"
```

全量运行的理论上界很大：1581 条任务、5 worker、每次 600 秒、最多 6 次 attempt，最坏约 316 小时；实际任务会提前完成或失败。模型 API token 和并发额度通常比云服务器费用更需要先设预算告警。

## 8A. 把服务器沉淀的 skill 搬回电脑

`skills` SQL 表是权威存储，Markdown 只是人工审阅快照。训练正常结束后，在服务器导出“已 promoted 且确实由训练 run 产生”的 skill：

```bash
cd ~/minecraft-agent-harness
PYTHONPATH=backend/src backend/.venv/bin/python scripts/export_skills.py \
  --learned-only \
  --output runs/exports/week10-24h-learned-skills.json
```

命令同时生成：

```text
runs/exports/week10-24h-learned-skills.json
runs/exports/week10-24h-learned-skills.json.sha256
```

在自己的电脑上执行下载和校验：

```bash
scp 'ubuntu@SERVER_IP:~/minecraft-agent-harness/runs/exports/week10-24h-learned-skills.json*' \
  ~/Downloads/
cd ~/Downloads
shasum -a 256 -c week10-24h-learned-skills.json.sha256
```

然后导入本机项目的 PostgreSQL skill library：

```bash
cd "/path/to/agent for minecraft"
docker compose up -d postgres redis
PYTHONPATH=backend/src backend/.venv/bin/python -m alembic upgrade head
PYTHONPATH=backend/src backend/.venv/bin/python scripts/import_skills.py \
  ~/Downloads/week10-24h-learned-skills.json
```

默认冲突策略是 `skip`，不会覆盖电脑上同名同版本 skill。确认需要以服务器版本覆盖时才显式使用 `--on-conflict replace`。

bundle 会保留 skill JSON 中的 `source_run_id`、source evidence、verifier stats 和 action plan，但如果本机没有对应远程 run，导入时会把 SQL 外键安全置空，避免外键失败。若还需要在电脑上查看每条 skill 的完整原始 trajectory、模型调用和 step，应该额外使用 `pg_dump` 迁移整个 PostgreSQL 数据库，而不仅是 skill bundle。

## 9. 停止与释放

训练停止后：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/stop_minecraft_server_pool.py
docker compose down
```

先确认 checkpoint、数据库 dump 和世界备份已经复制到对象存储或本机，再释放按量付费实例；仅在控制台“关机”不一定停止计算和云盘计费。
