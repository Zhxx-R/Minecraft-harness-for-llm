# Task Manifests

MineDojo tasks are imported into harness task manifests under `tasks/manifests/`.

The handoff package also includes:

- `catalog/minedojo_programmatic_tasks.jsonl`: the 1,581-task MineDojo catalog.
- `executable/minedojo_programmatic_tasks.jsonl`: the harness executable snapshot used by live training.
- `executable/minedojo_creative_tasks.jsonl`: all 1,560 authentic MineDojo creative tasks with external MineCLIP verifier metadata.
- `sources/minedojo/`: pinned official MineDojo YAML inputs and source revision metadata.

Week 6 includes a curated programmatic MineDojo-style subset under `tasks/manifests/minedojo/`. These manifests are used by `scripts/run_week6_benchmark.py` for deterministic runner, verifier, and report validation.

Each manifest should define:

- task id and source
- natural language goal
- allowed action scopes
- verifier type
- success criteria
- MineCLIP prompt or creative scoring metadata when relevant

Current Week 6 manifests also include `benchmark.initial_state` and `benchmark.scripted_actions`. These fields support dry-run CI and are not the final agent policy.

Creative manifests intentionally keep `guidance` as audit-only metadata. Their `creative_mineclip` verifier is evaluated after execution from first-person video frames; MineCLIP score and threshold are never injected into the agent's ReAct context. A task without a reviewed threshold remains `inconclusive`.
