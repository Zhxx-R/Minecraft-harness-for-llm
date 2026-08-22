# Week 4 开发文档

## 目标

Week 4 把 Week 3 的内存审计流升级成可查询的持久化层。当前 harness 已经具备 SQLAlchemy models、Alembic migration、SQL-backed trajectory recording、知识 chunk 入库、确定性检索审计，以及最小 checkpoint/resume 状态。

## 已交付变更

- 新增 SQLAlchemy models：
  - `runs`
  - `steps`
  - `trajectory_events`
  - `model_calls`
  - `runtime_errors`
  - `task_memories`
  - `skills`
  - `knowledge_chunks`
  - `checkpoints`
- 新增 Alembic 配置和初始迁移：
  - `alembic.ini`
  - `infra/migrations/env.py`
  - `infra/migrations/versions/0001_week4_persistence.py`
- 新增 SQL session factory：
  - `create_database_engine`
  - `create_session_factory`
  - `SessionLocal`
- 新增 `PersistentEvaluationRecorder`：
  - 继续保留 Week 3 的内存 `events`；
  - 把所有事件写入 `trajectory_events`；
  - 派生写入可查询的 `runs`、`steps`、`model_calls`、`runtime_errors`。
- 新增 `DatabaseStateStore`：
  - 把 checkpoint 写入 `checkpoints`；
  - 按 run id 读取最新 checkpoint。
- 新增 `DatabaseKnowledgeStore` 和 `DatabaseKnowledgeProvider`：
  - 把本地静态知识库导入 `knowledge_chunks`；
  - 使用确定性 lexical overlap 检索 chunks；
  - 术语和 recipe 解析仍复用确定性的 static provider。
- 启用 Postgres `vector` 扩展作为后续能力。Week 4 不生成 embeddings，也不使用 embedding-only RAG；vector/hybrid retrieval 是后续知识索引升级。
- 新增 `scripts/seed_knowledge_chunks.py`。
- `scripts/demo_week3_agent.py` 新增 `--persist-db` 模式。

## 本地运行

启动 Postgres 和 Redis：

```bash
docker compose up -d postgres redis
```

执行迁移：

```bash
PYTHON=/Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 make migrate-db
```

导入本地知识 chunks：

```bash
PYTHON=/Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 make seed-knowledge
```

运行带数据库持久化的 live demo：

```bash
./scripts/dev-worker.sh
```

另开终端：

```bash
PYTHONPATH=backend/src /Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  scripts/demo_week3_agent.py \
  --task inventory \
  --host localhost \
  --port <LAN_PORT> \
  --username HarnessAgent \
  --persist-db
```

脚本仍会把 JSON 审计文件写入 ignored 的 `runs/`，同时把可查询记录写入 Postgres。

## 持久化内容

- `runs`：run status、task id、task spec、开始/结束时间。
- `trajectory_events`：执行循环发出的所有 typed events。
- `steps`：每一步 observation、validated action、runtime result。
- `model_calls`：模型原始输出、解析后的 action、usage、source。
- `runtime_errors`：runtime/worker exception，会在继续抛出前记录。
- `knowledge_chunks`：本地 Minecraft terms、recipes、guide documents。
- `checkpoints`：可恢复的 run state snapshots。

## 知识检索边界

Week 4 持久化知识 chunks 和检索来源，但检索本身仍然是确定性的：

- canonical terms 和 recipes 使用 exact/static lookup；
- document snippets 使用 lexical overlap；
- `embedding` 字段是预留字段，当前为空；
- pgvector 已启用，用于后续 vector/hybrid indexes，不代表当前已经完成 RAG 层。

## Checkpoint/Resume 边界

Week 4 的 checkpoint 是最小版本。执行循环会按 `checkpoint_interval_steps` 保存状态。resume run 可以按同一个 `run_id` 读取最新 checkpoint，并从 `next_step_index` 继续。

它不会恢复 Minecraft 世界状态；它恢复的是 harness 侧 run state。这个边界足够先证明持久化契约，长任务 runtime 恢复后续再扩展。

## 验证

自动检查：

```bash
PYTHON=/Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 make ci
```

当前结果：

- Shared schema validation passed：3 个 schemas。
- Backend tests passed：22 个 tests。
- Worker TypeScript typecheck passed。
- Frontend TypeScript typecheck passed。
