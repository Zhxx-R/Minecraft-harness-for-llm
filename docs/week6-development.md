# Week 6 Development Document

## Goal

Week 6 adds the first MineDojo-derived task provider and benchmark runner. The scope is a curated, programmatic task subset for engineering validation, not a full MineDojo dataset import.

The benchmark is deliberately deterministic: it uses task manifests, scripted actions, an in-memory runtime, `ProgrammaticVerifier`, and JSON/Markdown reports. This validates the task/evaluation/reporting path before connecting the full live Mineflayer runtime and LLM policy to larger task sets.

## Delivered Changes

- Added `task-manifest.schema.json`.
- Added schema validation for all `tasks/manifests/**/*.json`.
- Added 10 curated MineDojo-style task manifests:
  - Harvest: `oak_log`, `cobblestone`, `dirt`, `sand`
  - TechTree: `oak_planks`, `crafting_table`, `stick`, `place_crafting_table`, `wooden_pickaxe`
  - Combat: `zombie`
- Implemented `MineDojoTaskProvider`:
  - `list_tasks()`
  - `load_task(task_id)`
  - `verify(run_state)`
- Implemented Week 6 benchmark infrastructure:
  - `BenchmarkConfig`
  - `BenchmarkRunner`
  - `ScriptedActionProvider`
  - `ScriptedBenchmarkRuntime`
  - JSON and Markdown report export
- Added CLI runner:

```bash
backend/.venv/bin/python scripts/run_week6_benchmark.py
```

## Task Manifest Contract

Each task manifest includes:

- `task_id`
- `source`
- `category`
- `family`
- `goal`
- `allowed_actions`
- `verifier`
- `success_criteria`
- `knowledge_tags`
- `benchmark.seed`
- `benchmark.max_steps`
- `benchmark.initial_state`
- `benchmark.scripted_actions`

The `benchmark.scripted_actions` field is only for deterministic Week 6 runner validation. It is not the final agent policy.

## Metrics

The benchmark report includes:

- success count and success rate
- total steps
- invalid action rate
- runtime crash rate
- input/output/total tokens
- estimated cost
- per-task verifier reason
- per-task event audit records

Scripted Week 6 runs use zero model tokens and zero cost. Live LLM runs will populate these fields through `model_action` events.

## How To Run

Run all 10 curated tasks:

```bash
backend/.venv/bin/python scripts/run_week6_benchmark.py
```

Run selected tasks:

```bash
backend/.venv/bin/python scripts/run_week6_benchmark.py \
  --task-id minedojo_harvest_oak_log \
  --task-id minedojo_techtree_wooden_pickaxe
```

Reports are written to:

```text
runs/week6/
```

## Verification

Automated checks:

```bash
make validate-schemas
make test-python
cd workers/mineflayer-worker && npm run typecheck
```

Expected benchmark smoke result:

```text
task_count: 10
success_count: 10
success_rate: 1.0
invalid_action_rate: 0.0
runtime_crash_rate: 0.0
```

## Current Boundary

- The task set is curated from MineDojo task categories; it is not a full import of MineDojo's complete task dataset.
- The Week 6 benchmark uses scripted actions to validate harness plumbing, not model intelligence.
- The runtime is in-memory for deterministic CI. Live Mineflayer benchmark execution should reuse the same task manifests and report format later.
- Creative tasks and MineCLIP scoring remain Week 11 scope.
