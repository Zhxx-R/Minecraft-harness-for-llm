# Week 9 Development Document: Training Scheduler and Parallel Exploration

## Scope

Week 9 adds a parallel training scheduler on top of the Week 6 deterministic MineDojo-style benchmark path. The goal is to validate the engineering mechanics before using live Mineflayer workers for expensive training:

- A task queue with auditable queued/running/final states.
- Per-task isolated memory namespaces.
- Resource budgets for max steps, max tokens, max runtime, and worker concurrency.
- A parallel runner that can execute 5-10 curated tasks without mixed logs or shared memory contamination.
- JSON and Markdown training reports under `runs/week9/`.
- A Redis queue adapter with the same queue contract as the default in-memory queue.

The default path still uses the scripted benchmark runtime. It is intentionally deterministic and CI-friendly. Live Minecraft training can reuse the same `TrainingRunner` boundary after the runtime factory is extended beyond Week 9.

## Backend Interfaces

Main implementation:

- `backend/src/mc_agent_harness/training/runner.py`
- `scripts/run_week9_training.py`
- `backend/tests/unit/test_week9_training.py`

Core objects:

- `TrainingBudget`: per-task resource limits and `worker_concurrency`.
- `TrainingJobConfig`: job id, model profile, runtime profile, seed, queue backend, and audit backend.
- `TrainingTaskRequest`: one queued task attempt with `memory_namespace`.
- `TrainingTaskOutcome`: terminal result and metrics for one task attempt.
- `TrainingQueueState`: auditable queue state for one task attempt.
- `InMemoryTrainingQueue`: default local asyncio queue for CI and local smoke tests.
- `RedisTrainingQueue`: Redis list/hash adapter for queue/state storage.
- `TrainingRunner`: enqueues tasks, starts workers, enforces budgets, and exports an aggregate report.

## Memory Isolation

Every task attempt receives a namespace:

```text
{job_id}:{task_id}:attempt-{attempt}
```

Week 9 reports this namespace for every outcome and queue state. The current deterministic runner does not yet write reflections to `task_memories`, but the namespace is now explicit and stable. Week 10/longer training runs can use it to ensure failed-attempt reflections are retrieved only for the same task attempt family.

## Queue and Audit Model

The queue records these states:

- `queued`
- `running`
- `succeeded`
- `failed`
- `runtime_crashed`
- `timeout`
- `token_budget_exceeded`

The in-memory queue is the default because it is stable in CI and needs no infrastructure. The Redis adapter stores queue items in a Redis list and mirrored state in a Redis hash:

```text
mc-agent-harness:training:{job_id}:queue
mc-agent-harness:training:{job_id}:states
```

The report artifact is the Week 9 source of truth for final training job status. Full Postgres-backed training job tables are still a later hardening step; run-level audit remains covered by the Week 4 persistent recorder for live agent runs.

## How To Run

Run all curated tasks with five local workers:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week9_training.py \
  --worker-concurrency 5
```

Run a selected subset:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week9_training.py \
  --task-id minedojo_harvest_oak_log \
  --task-id minedojo_techtree_oak_planks \
  --task-id minedojo_techtree_crafting_table \
  --worker-concurrency 3
```

Run with Redis:

```bash
make docker-up
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week9_training.py \
  --queue-backend redis \
  --redis-url redis://127.0.0.1:6379/0 \
  --worker-concurrency 5
```

Outputs:

```text
runs/week9/{job_id}.json
runs/week9/{job_id}.md
```

## Verification

Focused tests:

```bash
backend/.venv/bin/python -m pytest backend/tests/unit/test_week9_training.py
```

Smoke command:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week9_training.py \
  --worker-concurrency 5 \
  --output-dir runs/week9-smoke
```

Full project validation:

```bash
make ci
```

## Current Limits

- The default runner uses deterministic scripted tasks, not live Mineflayer workers.
- Redis is implemented as a queue/state adapter, not yet as a distributed multi-process worker fleet.
- Final training job status is exported as artifacts; dedicated Postgres `training_jobs` tables can be added when the dashboard needs first-class training views.
- Skill writing is not performed by Week 9 itself. The runner is designed to coexist with Week 7 skill promotion locks by keeping task memory namespaces isolated and training attempts explicit.
