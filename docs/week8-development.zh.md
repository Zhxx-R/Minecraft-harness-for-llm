# Week 8 开发文档：MVP Dashboard 与秋招展示版本

## 范围

Week 8 的目标是把 Week 4-7 已经持久化的审计数据做成一个可演示、可排查的 dashboard。当前实现包含四部分：

- Dashboard API：run、timeline event、model call、runtime error、skill、benchmark comparison。
- React dashboard：run 列表、run 详情 tab、skill review 操作、Week 8 对比表。
- Raw Mineflayer codegen baseline 的 sandbox 脚手架，和主 harness 执行链路隔离。
- 可重复运行的 comparison report 脚本，读取 Week 6 的实测 benchmark 输出。

## 后端 API

新增路由文件是 `backend/src/mc_agent_harness/api/routes/dashboard.py`。

公开接口：

- `GET /api/runs`：最近 run 列表，包含 step、event、model call、runtime error 计数。
- `GET /api/runs/{run_id}`：run 元数据和 task spec。
- `GET /api/runs/{run_id}/events?after_id=0`：按顺序返回 trajectory timeline。
- `GET /api/runs/{run_id}/model-calls`：模型原始输出、解析后的 action、usage、raw response。
- `GET /api/runs/{run_id}/runtime-errors`：worker/runtime 失败，关联 run 和 step。
- `GET /api/skills`：skill review 列表，包含所有生命周期状态。
- `GET /api/skills/{skill_id}`：完整 skill JSON spec。
- `POST /api/skills/{skill_id}/promote`：在 dashboard 中 promotion。
- `POST /api/skills/{skill_id}/deprecate`：在 dashboard 中 deprecation，并记录原因。
- `GET /api/benchmark-comparison`：从本地 artifact 组装 Week 8 对比视图。

Dashboard API 直接读取 SQL 审计表，不改变 agent 执行循环。skill promotion/deprecation 仍以 SQL `skills` 表作为 source of truth；如果 source run 存在，会额外写一条 trajectory event，保证 review 操作可审计。

## 前端

React dashboard 主要文件：

- `frontend/src/app/App.tsx`
- `frontend/src/api/client.ts`
- `frontend/src/shared/styles.css`

当前 UI 每 3 秒轮询一次。这是 Week 8 MVP 的实时观察实现；事件接口已经支持 `after_id` 增量读取，因此后续 Week 9/13 引入 Redis Streams 或 WebSocket fanout 时，可以只替换订阅层，不需要重写页面结构。

主要视图：

- Summary strip：run 数、promoted skill 数、runtime error 数、刷新时间。
- Run list：持久化 run、生命周期状态、step 数。
- Run audit detail：timeline、model calls、runtime errors 三个 tab。
- Skill review：promote/deprecate 操作。
- Week 8 comparison：raw codegen baseline、no-skill harness、skill-evolved harness。

如果前端和后端分开启动，需要设置：

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000
```

## Raw Codegen Baseline

baseline sandbox 位于 `backend/src/mc_agent_harness/evaluation/baselines.py`。

当前 sandbox 不会把任意 LLM 生成的 Mineflayer JS 直接接入真实 Minecraft worker。它只做：

- 源码大小限制。
- 高风险 Node API 的策略扫描，例如 `child_process`、`process`、`fs`、`net`、`eval`、动态 `Function`。
- 带超时的 `node --check` 语法检查。

这样可以安全地产生 Week 8 baseline artifact。候选 JS 如果违反策略或语法检查失败，会记作 raw-codegen baseline 自己的 crash/failure，但不会影响主 harness 服务。

## Comparison Report

comparison builder 位于 `backend/src/mc_agent_harness/evaluation/comparison.py`。

运行脚本：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week8_comparison.py
```

传入 raw JS baseline 候选：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week8_comparison.py \
  --raw-js path/to/generated_candidate.js
```

输出会保存到 `runs/week8/`，包含 JSON 和 Markdown。

当前数据质量标记：

- `raw_codegen_baseline`：没有传入 raw JS 候选时是 `sandbox_ready`。
- `no_skill_harness`：来自最新的 `runs/week6/*.json`，属于实测结果。
- `skill_evolved_harness`：在 skill replay benchmark 完成前是 `pending_replay`。

这是有意设计。dashboard 不会在 replay 实验完成前伪造 skill-evolved 的提升数据。

## 验证方式

聚焦验证：

```bash
backend/.venv/bin/python -m pytest backend/tests/unit/test_dashboard_api.py backend/tests/unit/test_week8_comparison.py
cd frontend && npm run typecheck
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week8_comparison.py
```

完整验证：

```bash
make ci
```

本地 dashboard：

```bash
./scripts/dev-backend.sh
cd frontend
VITE_API_BASE_URL=http://127.0.0.1:8000 npm run dev -- --host 127.0.0.1
```

浏览器打开 `http://127.0.0.1:5173`。

## 当前限制

- UI 目前使用轮询，还没有接 Redis Streams 或 WebSocket fanout。
- skill-evolved benchmark replay 还没有实测；对比表会明确显示 `pending_replay`。
- raw baseline sandbox 当前只做策略和语法检查；真正运行任意生成 JS 前，需要容器级或受限 worker 级隔离。
