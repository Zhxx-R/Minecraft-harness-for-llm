# Dashboard 门户与 Quick Start

## 目标

前端现在以操作门户为入口，不再默认把全部审计字段堆在同一个页面。首页提供五个明确工作区：

- `Quick Start`：选择任务、检查环境并启动一次真实 run。
- `Agent Runtime`：查看 agent、任务历史与逐轮 ReAct 证据。
- `Skill Review`：审查 skill 状态、来源和 promotion。
- `Creative Task Review`：以最终视频/截图为主完成人工审核，按需展开轨迹。
- `Evaluation Reports`：查看 harness 模式对比与实验指标。

页面地址使用 URL hash，可以直接收藏，例如：

- `http://127.0.0.1:5173/#/quick-start`
- `http://127.0.0.1:5173/#/runtime`
- `http://127.0.0.1:5173/#/skills`
- `http://127.0.0.1:5173/#/creative`

## 启动前提

Quick Start 不负责启动 Minecraft 客户端或服务端。使用前需要：

1. Minecraft 1.20.1 server 已启动，RCON 可用。
2. Minecraft 客户端已进入同一 server。
3. 后端 `.env` 已配置 `QWEN_API_KEY`、`QWEN_BASE_URL` 和 `MINECRAFT_RCON_PASSWORD`。
4. `MC_AGENT_SPECTATOR_PLAYER` 设置为客户端玩家名，或在页面中填写玩家名。
5. PostgreSQL/Redis 等项目依赖已按当前运行方式启动。

启动后端与前端：

```bash
cd "/Users/zmchen/Documents/agent for minecraft"
./scripts/dev-backend.sh
./scripts/dev-frontend.sh
```

打开 `http://127.0.0.1:5173`。

## Quick Start 流程

### 1. 选择客户端视角

- `Agent POV`：后端通过 RCON 把客户端切换为旁观者并跟随 bot。Creative task 强制使用此模式，因为最终视频需要是可信的 agent 第一视角证据。
- `Player View`：客户端维持普通玩家视角，bot 在同一 server 独立执行。适合只观察 programmatic task。

### 2. 配置本地环境

页面只接收非敏感配置：客户端玩家名、Minecraft/RCON 地址、步数、运行时间和布尔策略。API Key 与 RCON 密码始终由后端环境变量持有，前端只能看到“已配置/未配置”。

### 3. 选择任务

后端固定读取以下两个可信快照：

- `tasks/executable/minedojo_programmatic_tasks.jsonl`：1,581 条。
- `tasks/executable/minedojo_creative_tasks.jsonl`：1,560 条。

任务目录支持服务端搜索、类型/类别筛选和分页。页面只加载当前页，不会把 3,141 条完整 manifest 全部发送到浏览器。

`Random Task` 在后端从当前筛选集合中抽取最终任务。前端抽奖动画只展示过程，不决定结果，因此筛选语义可复现、不会选到隐藏范围外的任务。

选择任务后会显示 goal、category、verifier、biome、initial inventory、spawn mobs、reset plan 和可用原子动作。选择动作本身不会立即启动进程。

### 4. Preflight 与运行

`Check Environment` 会检查：

- Minecraft TCP 端口是否可达。
- RCON 是否已配置且可执行 `/list`。
- 填写的客户端玩家是否在线。
- Qwen provider 是否已配置。
- 任务与视角是否兼容。

`Start Task` 会再次执行同一组检查，防止检查后环境发生变化。一次本地环境只允许一个 Quick Start job，避免多个 bot/reset 流程争用同一世界。

Programmatic task 启动 `scripts/run_week10_live_training.py`；Creative task 启动 `scripts/run_week11_creative_task.py`，后者负责 Agent POV 录屏和离线 MineCLIP 评分。子进程日志、状态和 artifact 路径会显示在页面中，运行时可以取消或跳到 Runtime Audit。

## 安全边界

- 浏览器不能提交 shell 命令、脚本路径或任意 manifest 路径。
- 后端只允许两个固定任务快照和两个固定 runner。
- 子进程使用参数数组启动，不经过 shell。
- 启动/取消接口要求本地 dashboard 控制头。
- API 响应不返回 RCON 密码、Qwen API Key、完整命令或子进程环境变量。
- 日志读取有单次大小上限，前端只保留最近 80 KB。
- 任务 artifacts 默认写入 `runs/quick-start/<job-id>/`。

## API

- `GET /api/launcher/config`
- `GET /api/launcher/tasks`
- `GET /api/launcher/tasks/random`
- `GET /api/launcher/tasks/{task_id}`
- `POST /api/launcher/preflight`
- `POST /api/launcher/jobs`
- `GET /api/launcher/jobs`
- `GET /api/launcher/jobs/{job_id}`
- `GET /api/launcher/jobs/{job_id}/logs`
- `POST /api/launcher/jobs/{job_id}/cancel`

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/pytest -q \
  backend/tests/unit/test_launcher_catalog.py \
  backend/tests/unit/test_launcher_service.py \
  backend/tests/unit/test_launcher_api.py

cd frontend
npm run typecheck
npm run build
```

这些测试不会启动 Minecraft，也不会调用 LLM。真实任务只会在用户从 Quick Start 明确点击 `Start Task` 后运行。
