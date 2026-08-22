# Week 4 Development Document

## Goal

Week 4 turns Week 3's in-memory audit flow into a queryable persistence layer. The harness now has SQLAlchemy models, Alembic migrations, SQL-backed trajectory recording, knowledge chunk storage, deterministic retrieval audit, and minimal checkpoint/resume state.

## Delivered Changes

- Added SQLAlchemy models for:
  - `runs`
  - `steps`
  - `trajectory_events`
  - `model_calls`
  - `runtime_errors`
  - `task_memories`
  - `skills`
  - `knowledge_chunks`
  - `checkpoints`
- Added Alembic configuration and initial migration:
  - `alembic.ini`
  - `infra/migrations/env.py`
  - `infra/migrations/versions/0001_week4_persistence.py`
- Added SQL session factory:
  - `create_database_engine`
  - `create_session_factory`
  - `SessionLocal`
- Added `PersistentEvaluationRecorder`:
  - keeps Week 3 in-memory `events`;
  - persists all events to `trajectory_events`;
  - derives queryable rows for `runs`, `steps`, `model_calls`, and `runtime_errors`.
- Added `DatabaseStateStore`:
  - saves checkpoints to `checkpoints`;
  - loads the latest checkpoint by run id.
- Added `DatabaseKnowledgeStore` and `DatabaseKnowledgeProvider`:
  - seeds local static knowledge into `knowledge_chunks`;
  - retrieves chunks with deterministic lexical overlap;
  - keeps term and recipe resolution backed by the deterministic static provider.
- Enabled the Postgres `vector` extension as a future capability. Week 4 does not generate embeddings or use embedding-only RAG; vector/hybrid retrieval is a later knowledge-indexing upgrade.
- Added `scripts/seed_knowledge_chunks.py`.
- Added `--persist-db` mode to `scripts/demo_week3_agent.py`.

## Run Locally

Start Postgres and Redis:

```bash
docker compose up -d postgres redis
```

Run migrations:

```bash
PYTHON=/Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 make migrate-db
```

Seed local knowledge chunks:

```bash
PYTHON=/Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 make seed-knowledge
```

Run a live persisted demo:

```bash
./scripts/dev-worker.sh
```

In another terminal:

```bash
PYTHONPATH=backend/src /Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  scripts/demo_week3_agent.py \
  --task inventory \
  --host localhost \
  --port <LAN_PORT> \
  --username HarnessAgent \
  --persist-db
```

The script still writes the JSON audit file under ignored `runs/`, and additionally persists queryable rows to Postgres.

## What Gets Persisted

- `runs`: run status, task id, task spec, start/finish timestamps.
- `trajectory_events`: every typed event emitted by the loop.
- `steps`: observation, validated action, and runtime result per step.
- `model_calls`: model raw output, parsed action, usage, source.
- `runtime_errors`: runtime/worker exceptions recorded before propagation.
- `knowledge_chunks`: local Minecraft terms, recipes, and guide documents.
- `checkpoints`: latest recoverable run state snapshots.

## Knowledge Retrieval Boundary

Week 4 persists knowledge chunks and retrieval sources, but retrieval remains deterministic:

- canonical terms and recipes use exact/static lookup;
- document snippets use lexical overlap;
- the `embedding` field is reserved and currently empty;
- pgvector is enabled for future vector/hybrid indexes, not treated as an already completed RAG layer.

## Checkpoint/Resume Boundary

Checkpointing is minimal in Week 4. The execution loop saves state every `checkpoint_interval_steps`. A resume run can load the latest checkpoint for the same `run_id` and start from `next_step_index`.

This does not restore the Minecraft world. It restores harness-side run state and is enough to prove the persistence contract before long-horizon runtime recovery is implemented.

## Verification

Automated checks:

```bash
PYTHON=/Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 make ci
```

Current result:

- Shared schema validation passed: 3 schemas.
- Backend tests passed: 22 tests.
- Worker TypeScript typecheck passed.
- Frontend TypeScript typecheck passed.
