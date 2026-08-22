# Week 9 开发文档：训练调度与并行探索

## 范围

Week 9 在 Week 6 的确定性 MineDojo-style benchmark 路径上增加并行训练调度层。这里先验证工程机制，再把昂贵且不稳定的 live Mineflayer 多 worker 训练接进来：

- 带审计状态的任务队列：queued、running、terminal status。
- 每个任务 attempt 独立 memory namespace。
- 资源预算：每任务最大 step、最大 token、最大运行时间、worker 并发数。
- 并行 runner：可以一次跑 5-10 个 curated task，避免日志串扰和任务记忆污染。
- 输出 JSON/Markdown 训练报告。
- Redis 队列适配器，和默认内存队列使用同一套接口。

默认执行路径仍然使用 scripted benchmark runtime。这是有意设计：它稳定、可复现、适合 CI。后续把真实 Mineflayer worker 接入训练时，可以复用同一个 `TrainingRunner` 边界，只替换 runtime factory。

## 后端接口

主要实现文件：

- `backend/src/mc_agent_harness/training/runner.py`
- `scripts/run_week9_training.py`
- `backend/tests/unit/test_week9_training.py`

核心对象：

- `TrainingBudget`：每个 task attempt 的资源预算和 `worker_concurrency`。
- `TrainingJobConfig`：job id、model profile、runtime profile、seed、queue backend、audit backend。
- `TrainingTaskRequest`：一个入队的任务 attempt，包含 `memory_namespace`。
- `TrainingTaskOutcome`：一个任务 attempt 的最终结果和指标。
- `TrainingQueueState`：一个任务 attempt 的可审计队列状态。
- `InMemoryTrainingQueue`：默认本地 asyncio 队列，用于 CI 和本地 smoke test。
- `RedisTrainingQueue`：Redis list/hash 适配器，用于队列和状态存储。
- `TrainingRunner`：负责任务入队、启动 worker、执行预算约束、汇总训练报告。

## 记忆隔离

每个任务 attempt 都会得到一个 namespace：

```text
{job_id}:{task_id}:attempt-{attempt}
```

Week 9 会在每个 outcome 和 queue state 中记录这个 namespace。当前 deterministic runner 还不会把失败反思写入 `task_memories` 表，但 namespace 已经稳定显式。Week 10 或后续真实训练阶段可以直接用它保证失败经验只被同一任务族检索，不污染其他任务。

## 队列与审计模型

队列状态包括：

- `queued`
- `running`
- `succeeded`
- `failed`
- `runtime_crashed`
- `timeout`
- `token_budget_exceeded`

默认使用内存队列，因为它不需要基础设施，适合 CI。Redis 适配器会把任务写入 Redis list，把状态镜像写入 Redis hash：

```text
mc-agent-harness:training:{job_id}:queue
mc-agent-harness:training:{job_id}:states
```

Week 9 的最终训练状态以 report artifact 作为 source of truth。专门的 Postgres `training_jobs` 表还没有加，等 dashboard 需要一等训练视图时再加更合理。真实 agent run 的逐步审计仍然由 Week 4 的 persistent recorder 负责。

## 如何运行

用 5 个本地 worker 跑全部 curated task：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week9_training.py \
  --worker-concurrency 5
```

只跑指定任务子集：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week9_training.py \
  --task-id minedojo_harvest_oak_log \
  --task-id minedojo_techtree_oak_planks \
  --task-id minedojo_techtree_crafting_table \
  --worker-concurrency 3
```

使用 Redis 队列：

```bash
make docker-up
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week9_training.py \
  --queue-backend redis \
  --redis-url redis://127.0.0.1:6379/0 \
  --worker-concurrency 5
```

输出文件：

```text
runs/week9/{job_id}.json
runs/week9/{job_id}.md
```

## 验证方式

聚焦测试：

```bash
backend/.venv/bin/python -m pytest backend/tests/unit/test_week9_training.py
```

Smoke 命令：

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week9_training.py \
  --worker-concurrency 5 \
  --output-dir runs/week9-smoke
```

完整验证：

```bash
make ci
```

## 当前限制

- 默认 runner 使用 deterministic scripted task，不是真实 Mineflayer worker。
- Redis 目前是队列/状态适配器，不是完整的分布式多进程 worker fleet。
- 最终训练 job 状态以 artifact 形式输出；如果 dashboard 后续需要训练视图，再加专门的 Postgres 表。
- Week 9 自身不写 skill。它和 Week 7 skill promotion lock 的关系是：训练 attempt 显式化，任务记忆 namespace 隔离，后续接入 skill 写入时可以避免不同任务互相污染。
