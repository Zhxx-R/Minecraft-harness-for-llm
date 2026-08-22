# Week 6 开发文档

## 目标

Week 6 加入第一版 MineDojo-derived task provider 和 benchmark runner。当前范围是一个 curated programmatic 小任务集，用来验证工程链路，不是完整 MineDojo 数据集导入。

Benchmark 被设计成确定性流程：使用 task manifests、scripted actions、内存 runtime、`ProgrammaticVerifier` 和 JSON/Markdown 报告。这样可以先验证任务、评测、报告和审计路径，再把更大的任务集接到真实 Mineflayer runtime 和 LLM policy 上。

## 已交付变更

- 新增 `task-manifest.schema.json`。
- `make validate-schemas` 会校验所有 `tasks/manifests/**/*.json`。
- 新增 10 个 curated MineDojo-style task manifests：
  - Harvest：`oak_log`、`cobblestone`、`dirt`、`sand`
  - TechTree：`oak_planks`、`crafting_table`、`stick`、`place_crafting_table`、`wooden_pickaxe`
  - Combat：`zombie`
- 实现 `MineDojoTaskProvider`：
  - `list_tasks()`
  - `load_task(task_id)`
  - `verify(run_state)`
- 实现 Week 6 benchmark 基础设施：
  - `BenchmarkConfig`
  - `BenchmarkRunner`
  - `ScriptedActionProvider`
  - `ScriptedBenchmarkRuntime`
  - JSON 和 Markdown 报告导出
- 新增 CLI runner：

```bash
backend/.venv/bin/python scripts/run_week6_benchmark.py
```

## Task Manifest Contract

每个 task manifest 包含：

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

`benchmark.scripted_actions` 只用于 Week 6 的确定性 runner 验证，不是最终 agent policy。

## 指标

Benchmark report 包含：

- success count 和 success rate
- total steps
- invalid action rate
- runtime crash rate
- input/output/total tokens
- estimated cost
- 每个 task 的 verifier reason
- 每个 task 的 event audit records

Week 6 scripted run 不调用真实模型，因此 model tokens 和 cost 都是 0。后续 live LLM run 会通过 `model_action` events 填充这些字段。

## 如何运行

运行全部 10 个 curated tasks：

```bash
backend/.venv/bin/python scripts/run_week6_benchmark.py
```

运行指定 tasks：

```bash
backend/.venv/bin/python scripts/run_week6_benchmark.py \
  --task-id minedojo_harvest_oak_log \
  --task-id minedojo_techtree_wooden_pickaxe
```

报告输出目录：

```text
runs/week6/
```

## 验证

自动检查：

```bash
make validate-schemas
make test-python
cd workers/mineflayer-worker && npm run typecheck
```

预期 benchmark smoke result：

```text
task_count: 10
success_count: 10
success_rate: 1.0
invalid_action_rate: 0.0
runtime_crash_rate: 0.0
```

## 当前边界

- 当前 task set 是从 MineDojo task categories 中整理出的 curated 小集合，不是完整 MineDojo 数据集导入。
- Week 6 benchmark 使用 scripted actions 验证 harness 链路，不代表模型智能。
- Runtime 是用于 CI 的内存实现。后续真实 Mineflayer benchmark 应复用同一套 task manifests 和 report 格式。
- Creative tasks 和 MineCLIP scoring 仍属于 Week 11 范围。
