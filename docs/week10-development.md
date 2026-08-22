# Week 10 Development Document: Programmatic Catalog, Diverse Batches, Skill Dedup

## Scope

Week 10 turns the Week 9 parallel runner into a better skill-training foundation. It adds:

- A local snapshot of MineDojo's full 1581-task programmatic catalog.
- A catalog importer from MineDojo's official task description files.
- A task similarity scorer and diversity-aware batch planner.
- Optional diverse-batch selection in the Week 9 training runner.
- Skill candidate duplicate detection before promotion.
- A live parallel programmatic training runner that can start multiple Mineflayer workers, run executable programmatic manifests, and update the SQL skill library.
- Prompt layering and primitive harvest actions so the agent learns `scan -> move -> dig -> collect -> verify` procedure skills instead of depending on a worker macro action.

The full catalog is not the same as the curated executable manifests. The catalog contains all official task prompts/guidance and is used for selection, grouping, and future live training. The curated manifests under `tasks/manifests/` remain the deterministic CI/smoke set because they include scripted action traces and programmatic verifiers.

## Week 10C: MineDojo Executable Alignment

Week 10C adds an executable manifest adapter for the full MineDojo programmatic catalog. It first matches official `tasks_specs.yaml` templates, fills template bindings from each expanded task id, and emits harness-native verifier/reset metadata:

- Harvest: inventory-delta verifier.
- Combat: kill-entity-stat delta verifier plus reset-time initial mob spawning.
- TechTree: use-item-stat delta verifier.
- Survival: alive-time delta verifier.

Dry-run the full adapter without writing generated manifests:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/build_minedojo_executable_manifests.py \
  --pretty
```

Write a generated JSONL snapshot that `MineDojoTaskProvider` can load directly:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/build_minedojo_executable_manifests.py \
  --output-jsonl runs/minedojo_executable_manifests.jsonl \
  --summary-path runs/minedojo_executable_manifests.summary.json \
  --pretty
```

The live runner can then point `--manifest-dir` at that JSONL. RCON reset now consumes manifest `reset_plan` fields and audits command plans/results in `environment_reset` and `run_started.reset_result`.

Skill semantics also changed: `execute_skill` is not exposed by default in Week 10 training. Skills are contextual procedural memories with `strategy_summary`, `parameterized_plan`, `recovery_policy`, `source_evidence`, and `verifier_stats`; source `action_plan` is retained for audit/replay, not default macro execution.

For isolated multi-port local training, start a generated server pool:

```bash
MINECRAFT_RCON_PASSWORD=<PASSWORD> \
PYTHONPATH=backend/src backend/.venv/bin/python scripts/start_minecraft_server_pool.py \
  --server-count 2 \
  --first-server-port 25565 \
  --first-rcon-port 25575 \
  --heap-gb 2.5
```

Stop it with:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/stop_minecraft_server_pool.py
```

Formal parallel mode now enforces one Minecraft server, RCON endpoint, and world directory per worker. Shared-world parallelism requires the explicit development-only `--allow-shared-server-workers` override. The live report records the resolved placement instead of treating `server_pool_state.json` as display-only metadata.

The 2026-07-12 local two-server smoke used about 2.8GB RSS per Java process. The conservative estimate including two workers and service overhead is 9.7GB, so two servers remain the default on the 32GB development machine.

## MineDojo Sources

The importer uses these upstream files:

- `programmatic_tasks.yaml`: official prompt, guidance, and category for 1581 programmatic tasks.
- `tasks_specs.yaml`: official MineDojo programmatic task specification source.
- `tasks_suite.yaml`: official standard/difficult benchmark subset labels.

Local outputs:

- `tasks/catalog/minedojo_programmatic_tasks.jsonl`
- `tasks/catalog/minedojo_programmatic_tasks.summary.json`

Current category counts:

- Combat: 471
- Harvest: 895
- Survival: 2
- TechTree: 213

## How To Import

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/import_minedojo_programmatic_catalog.py
```

Expected summary:

```json
{
  "task_count": 1581,
  "categories": {
    "combat": 471,
    "harvest": 895,
    "survival": 2,
    "techtree": 213
  }
}
```

The script can also read local YAML files through `--programmatic-file` and `--tasks-suite-file`, which keeps CI independent from the network.

## Diverse Batch Planning

Task similarity is computed from:

- category/family
- goal text
- knowledge tags
- allowed action set
- verifier target or MineDojo task target tokens

The planner greedily selects the task with the lowest maximum similarity to already selected tasks. This is intended for parallel single-agent skill training:

```text
worker1 -> task A -> candidate skill
worker2 -> task B -> candidate skill
worker3 -> task C -> candidate skill
```

The agents do not communicate. Diversity reduces duplicate skill candidates and resource conflicts during a training epoch.

Plan a batch from the full catalog:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/plan_week10_diverse_batch.py \
  --batch-size 10 \
  --max-task-similarity 0.45
```

Run a diverse executable smoke batch from curated manifests:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week9_training.py \
  --diverse-batch-size 5 \
  --worker-concurrency 5 \
  --output-dir runs/week10-smoke
```

## Live Parallel Training

`LiveTrainingRunner` is the Week 10B live path. It keeps the design as parallel single-agent training, not multi-agent:

```text
worker-1 -> bot username A -> task A -> verifier -> skill candidate
worker-2 -> bot username B -> task B -> verifier -> skill candidate
```

Workers do not send messages to each other. They only share the SQL skill library, and the write path goes through candidate creation, duplicate detection, and optional promotion.

Main files:

- `backend/src/mc_agent_harness/training/live_runner.py`
- `scripts/run_week10_live_training.py`

Scripted live smoke against a Minecraft LAN server:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <LAN_PORT> \
  --task-id minedojo_harvest_oak_log \
  --worker-concurrency 1 \
  --scripted \
  --auto-promote \
  --start-delay-sec 30 \
  --max-steps-per-task 5
```

Parallel live smoke with two isolated servers:

```bash
export MINECRAFT_RCON_PASSWORD=<PASSWORD>

PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --server-pool-state infra/minecraft-server-pool/server_pool_state.json \
  --manifest-dir tasks/executable/minedojo_programmatic_tasks.jsonl \
  --task-id combat_chicken_forest_barehand \
  --task-id harvest_1_dirt \
  --worker-concurrency 2 \
  --scripted \
  --rcon-reset \
  --rcon-random-teleport-when-biome-missing \
  --clear-all-inventory-on-reset \
  --max-steps-per-task 1 \
  --max-runtime-sec-per-task 90
```

When a manifest has `biome_hint`, reset audits `/locate biome` plus `/spreadplayers` and caches the coordinates per server. Tasks without a biome hint can use the random-teleport fallback without overriding specified-biome tasks.

## Formal 100-task pilot

Build and persist the exact task/wave plan without starting Minecraft or calling the model:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_formal_batch.py \
  --dry-run \
  --task-count 100 \
  --worker-concurrency 2 \
  --max-task-retries 5
```

The executable snapshot is sampled proportionally as `57 harvest / 30 combat / 13 techtree`. Low-similarity waves run at most two tasks concurrently; when no second task satisfies the threshold, the scheduler emits a single-task wave instead of forcing a near-duplicate pair.

Run the formal pilot with:

```bash
export MINECRAFT_RCON_PASSWORD=<PASSWORD>
make docker-up
make minecraft-pool-up
make week10-formal-100
```

`--max-task-retries 5` means one initial attempt plus at most five retries, stopping immediately after success. `attempt_outcomes` retains every attempt, while `outcomes` contains one final result per task. Formal multi-worker LLM runs default to PostgreSQL; SQLite requires a single worker or an explicit development override.

An atomic `week10_formal_batch.checkpoint.json` is written after every completed wave. Resume from the original output directory with:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_formal_batch.py \
  --resume \
  --output-dir runs/formal/<original-directory>
```

Resume validates task ids, the wave plan, and skill/learning snapshot revisions while execution remains incomplete. Only complete waves are checkpointed; an in-flight partial wave is rerun. Skill candidate creation is idempotent by `source_run_id`, allowing interrupted finalization to continue safely.

The promoted-skill snapshot is frozen for the entire batch. Failure classification, recovery validation, deduplication, and optional promotion run only after every wave and retry has completed.

Every attempt and the aggregate report include model call and input/output/total token counts. Malformed JSON, invalid actions, and failed repair calls are also persisted in `model_calls`, so cost reporting does not count only the final valid action.

For manual setup, `minedojo_harvest_dirt` can use natural ground or manually placed dirt. The worker mines the nearby `dirt` target as-is, then moves horizontally above the dropped item to pick it up after a one-block-deep hole is created.

LLM version verification:

Scripted smoke only verifies worker concurrency and the training chain; it does not verify the model. For real LLM validation, first run a model-only check without Minecraft:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/verify_llm_model.py
```

The script writes `runs/llm_verify_<timestamp>.json` and prints:

- `request_model`: the `MODEL_DEFAULT` or `--model` used for the request.
- `response_model`: the model field returned by the provider, if present.
- `provider`: the current `ModelProfile.provider`.
- `usage`: token usage returned by the provider.
- `action`: the model action after harness schema validation.

To validate a model id without changing `.env`:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/verify_llm_model.py \
  --model qwen3.7-plus
```

Real LLM live training removes `--scripted`:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <LAN_PORT> \
  --diverse-batch-size 2 \
  --worker-concurrency 2 \
  --auto-promote \
  --start-delay-sec 30 \
  --max-steps-per-task 8
```

## Prompt And Primitive Harvest Actions

Week 10 now also adjusts the prompt/action contract to remove reusable procedure from coarse worker macros. Harvest tasks should teach the skill library how to find a target, move near it, dig it, move to the drop, wait for pickup, and verify inventory.

The prompt is assembled in three layers, with knowledge retrieval agent-selected by default:

- Static system prompt: role, behavior rules, no raw code, no fabricated state, exploration policy, and skill-use policy.
- Stable harness contract: allowed primitives, knowledge-tool boundaries, runtime hints, and the decision envelope. It is invariant across turns for one action profile.
- Dynamic user payload: task, state summary, compact evidence, task memory, skills, learning candidates, and run context.

The static prompt and stable contract form one `role=system` message; all per-turn data stays in `role=user`. This keeps the prefix cacheable by OpenAI/Qwen-style services. Every step still records the complete prompt in `context_built`:

```sql
select payload
from trajectory_events
where event_type = 'context_built'
order by id
limit 1;
```

You can inspect a task's first-step prompt without connecting to Minecraft:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/dump_agent_prompt.py \
  --task-id minedojo_harvest_oak_log \
  --pretty
```

Harvest curated manifests now expose these primitive actions by default:

- `scan_blocks`: scan nearby target blocks without changing the world.
- `move_to`: move near a coordinate.
- `dig_block_at`: dig one explicit block coordinate.
- `scan_dropped_items`: scan nearby dropped item entities without moving.
- `wait_ticks`: wait for vanilla pickup and short-lived world-state updates.
- `query_inventory`: confirm inventory state.
- `request_visual_snapshot`: request visual evidence when text observation is insufficient.

The old collection macros are removed from the schema, worker dispatch, prompt contract, tests, and smoke scripts. `minedojo_harvest_oak_log`, `minedojo_harvest_dirt`, `minedojo_harvest_sand`, and `minedojo_harvest_cobblestone` now use primitive scripted traces. A successful trajectory is expected to settle into a skill shaped like:

```text
scan_blocks(target) -> move_to(block_position) -> dig_block_at(block_position) -> scan_dropped_items(drop) -> move_to(drop_position) -> wait_ticks -> query_inventory
```

Current candidates preserve concrete source action args in `action_plan` for replay audit, while `validation.parameterized_plan` stores the generalized review plan, for example selecting targets from scan results or recovering no-path moves with `nearest_reachable_position`. A later skill executor must bind that parameterized plan to the current observation before execution.

Live training enables the inventory delta verifier: `inventory_contains` no longer checks only the final inventory. It requires the target item count to increase during this run, from the first observation to the final state. Clear bot inventories before testing; if the first observation already contains `oak_log` or `dirt`, the run will not pass and no skill will be promoted from it.

Skill candidates are now gated by the initial `SkillCreationPolicy` and `SkillSummarizer` pipeline: verified success is necessary but not sufficient. Non-trivial workflows, recovered failures, reusable task-family procedures, low duplication, or meaningful step/token savings are required before a candidate is created or updated. Candidate action plans are extracted from successful progress actions such as `scan_blocks`, `scan_dropped_items`, `move_to`, `dig_block_at`, `wait_ticks`, `craft_item`, `place_block`, `fight_entity`, `use_item`, and `execute_skill`. Pure `query_inventory`, pure `request_visual_snapshot`, or simple recipe traces already covered by the knowledge base are skipped by default. The current summarizer is deterministic; a later version can use the LLM to summarize strategy, triggers, and recovery boundaries.

## Agent-driven Knowledge Retrieval

Knowledge is no longer retrieved and spliced into context by `ContextManager` by default. The current implementation exposes the knowledge base as read-only harness tools:

- `resolve_terms(text)`: resolve canonical IDs, aliases, and types.
- `get_recipe(item_id)`: return recipe inputs, output count, and station requirements.
- `retrieve_docs(query, limit, scope)`: retrieve short chunks from local Minecraft wiki summaries, Mineflayer operation guides, or project knowledge.

The model decides whether and what to retrieve; the harness enforces safety:

- Offline knowledge is the default. Web Search stays disabled by default.
- Online wiki access requires domain allowlists, length budgets, HTML/script cleanup, prompt-injection downgrading, and source attribution.
- Every knowledge tool call is recorded as a `knowledge_tool_call` in `trajectory_events`, including query, args, source ids, returned summary, and truncation budget.
- Exact deterministic queries use a run-scoped cache; cache hits remain audited and do not call the provider again.
- Results first return as the next ReAct observation, then enter low-priority `run_context.knowledge`.
- Knowledge does not enter durable trajectory context. Compression reduces it to summaries and then evicts it; the model may query it again.

## Run-scoped Hierarchical Context

Raw observations, decisions, and action results remain fully persisted. Prompt-only history is compressed as follows: the latest action stays in `compact_evidence.previous_step`; recent actions keep typed evidence; older actions merge into navigation, collection, processing, combat, and interaction segments; aggressive and episode modes retain only action counts, progress signals, and recent failures. Knowledge is managed separately and is evicted before non-repeatable trajectory evidence. `RunContextMemory` is checkpointed for resume. The default run-context budget is 12,000 characters, including at most 3,500 lower-priority knowledge characters.

## Server-backed `dig_block_at` Drop Evidence

Vanilla RCON has no subscription that directly associates a dig action with an item drop. The worker instead compares server-synchronized inventory and dropped-item entities before and after the bounded dig window. Results distinguish block removal from actual drop evidence: `inventory_gained` includes exact positive `inventory_delta`; `drop_entity_observed` includes new nearby `spawned_drops`; `no_drop_observed` makes no inferred drop claim; and a timeout returns `dig_incomplete_no_drop_claim`. `drop_evidence_source=minecraft_server_entity_packets_and_inventory` records the provenance.

Outputs:

- `runs/week10_live_training_<timestamp>.json`
- `runs/week10_live_training_<timestamp>.sqlite3`

The SQLite DB contains `runs`, `steps`, `trajectory_events`, `model_calls`, `runtime_errors`, `task_memories`, and `skills`. If a verifier fails, the runner writes a task-local memory note under the worker's memory namespace. If a verifier succeeds, the runner first applies skill creation policy and summarization, checks duplicates with `SkillLibrary.find_duplicates(...)`, and promotes only after replay/deduplication/quality gates pass.

## Skill Candidate Dedup

`SkillCandidateDeduper` compares candidate skills with existing draft/validated/staged/promoted skills using:

- action types
- action targets
- triggers
- task scope
- dependencies
- name tokens

`SkillLibrary.find_duplicates(candidate, threshold=0.82)` returns near-duplicate matches. It does not automatically block promotion yet; that remains the job of a future promotion coordinator. This keeps Week 10 safe and avoids changing Week 7 lifecycle semantics.

## Verification

Prefer the automated runner first. By default it does not connect to Minecraft. It runs worker typecheck, Week 10 focused tests, JSON schema validation, prompt dumps, and the deterministic benchmark, then preserves every artifact in one result directory:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/test_week10_automated.py
```

Default output directory:

```text
runs/week10_automated_<timestamp>/
```

Directory contents:

- `summary.json`: machine-readable aggregate result.
- `summary.md`: human-readable summary.
- `metadata.json`: test args, git status, and timestamp.
- `logs/`: stdout/stderr for every command.
- `prompts/`: full prompt dumps.
- `benchmark/`: deterministic benchmark JSON/Markdown reports.

Use a fixed output directory:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/test_week10_automated.py \
  --output-dir runs/week10_manual_check
```

Add a live scripted Minecraft test when a LAN server is open:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/test_week10_automated.py \
  --live-port <LAN_PORT> \
  --live-scripted \
  --auto-promote \
  --clear-inventory-on-reset \
  --start-delay-sec 30
```

Run the real LLM path:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/test_week10_automated.py \
  --live-port <LAN_PORT> \
  --live-llm \
  --auto-promote \
  --clear-inventory-on-reset \
  --start-delay-sec 30
```

`--live-llm` first runs `verify_llm_model.py`, then runs real LLM live training. Live artifacts include:

- `live_scripted/week10_live_training.json`
- `live_scripted/week10_live_training.sqlite3`
- `live_llm/week10_live_training.json`
- `live_llm/week10_live_training.sqlite3`

### Live Reset Inventory Clearing

To work with the inventory delta verifier, the live runner can clear the worker bot inventory during reset. The common command is:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <LAN_PORT> \
  --task-id minedojo_harvest_oak_log \
  --clear-inventory-on-reset \
  --auto-promote \
  --start-delay-sec 30 \
  --max-steps-per-task 8
```

When only `--clear-inventory-on-reset` is provided, the runner infers target items from the verifier, for example `oak_log`, and writes this into `runtime.reset_policy`:

```json
{"clear_inventory": {"enabled": true, "mode": "items", "items": ["oak_log"], "drop_fallback": true}}
```

You can explicitly choose items:

```bash
--clear-inventory-on-reset --clear-item oak_log --clear-item dirt
```

Or clear the full inventory:

```bash
--clear-all-inventory-on-reset
```

The worker first tries Minecraft `/clear` commands. This is closest to MineDojo reset semantics, but MineDojo executes commands through the Malmo/Minecraft server bridge. The current Mineflayer worker is a normal player connected to a LAN/server world, so it may not have command permission. When the command clear is not verified, the worker records server feedback and enables `drop_fallback` by default: it uses Mineflayer player APIs to `tossStack` matching items or the full inventory so the agent does not stop because stale target items are already in inventory.

`drop_fallback` does not require OP permission, but it only drops items into the world. It cannot delete dropped entities like a server-side `/clear`/cleanup command. For real training, prefer one of these server-authorized reset paths:

- Open LAN with cheats enabled and grant the bot username OP/command permission.
- Use a dedicated Minecraft server and add training bot users to `ops.json`.
- Later integrate a MineDojo/Malmo-style server command channel or reset mod/datapack for inventory clearing, item cleanup, time/weather/biome setup, and other reset controls.

To disable the drop fallback:

```bash
--no-reset-drop-fallback
```

### RCON Server Reset

To better match the MineDojo/Malmo reset permission model, the live runner can execute harness-owned server commands through Minecraft RCON. RCON commands are not exposed in the prompt and are not LLM actions; they belong only to environment reset and are recorded under `environment_reset.server_command_reset`.

Enable RCON in the Minecraft server:

```properties
enable-rcon=true
rcon.port=25575
rcon.password=<your_password>
```

Use environment variables locally so the password is not written into task specs:

```bash
export MINECRAFT_RCON_PASSWORD=<your_password>
export MINECRAFT_RCON_HOST=localhost
export MINECRAFT_RCON_PORT=25575
```

Single-task live LLM test:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <LAN_PORT_OR_SERVER_PORT> \
  --task-id minedojo_harvest_oak_log \
  --worker-concurrency 1 \
  --max-steps-per-task 20 \
  --clear-all-inventory-on-reset \
  --rcon-reset \
  --rcon-set-time day \
  --rcon-set-weather clear \
  --auto-promote
```

Parallel smoke:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <LAN_PORT_OR_SERVER_PORT> \
  --task-id minedojo_harvest_oak_log \
  --task-id minedojo_harvest_dirt \
  --worker-concurrency 2 \
  --max-steps-per-task 20 \
  --clear-all-inventory-on-reset \
  --rcon-reset \
  --rcon-set-time day \
  --rcon-set-weather clear \
  --auto-promote
```

Random teleport on reset:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <LAN_PORT_OR_SERVER_PORT> \
  --task-id harvest_1_feather \
  --worker-concurrency 1 \
  --max-steps-per-task 40 \
  --clear-all-inventory-on-reset \
  --rcon-reset \
  --rcon-set-time day \
  --rcon-set-weather clear \
  --rcon-random-teleport-on-reset \
  --rcon-random-teleport-center-x 0 \
  --rcon-random-teleport-center-z 0 \
  --rcon-random-teleport-max-range 500
```

`--rcon-random-teleport-on-reset` injects a reset-plan `random_teleport` field and uses Minecraft `/spreadplayers` through RCON. It removes manifest `start_position` by default so a following `/tp` command cannot override the random location. Use `--rcon-random-teleport-keep-start-position` only when you intentionally want both commands in the reset plan. This improves task isolation and observation diversity, but it does not guarantee the target biome/resource is nearby.

By default, RCON reset generates commands from the worker username and reset policy:

```text
/clear <worker_username>
/kill @e[type=item]
/kill @e[tag=mc_agent_owner_<normalized_worker_username>]
```

For target-item reset mode, it generates:

```text
/clear <worker_username> minecraft:<target_item>
/kill @e[type=item]
/kill @e[tag=mc_agent_owner_<normalized_worker_username>]
```

Mobs created by a reset plan receive `mc_agent_task_mob`, a worker owner tag, and a stable task tag derived from `task_id`. The next reset removes only mobs created for that worker's previous task; it does not remove natural mobs or interfere with another worker sharing the server. The task tag preserves provenance, and both cleanup and tagged summon commands are retained in the `environment_reset` audit event.

RCON solves server-authorized reset automation. It does not by itself complete full MineDojo programmatic task execution mapping. The Week 10 final chain still needs catalog tasks converted into executable harness manifests: target item/entity/block, allowed actions, verifier, required initial inventory, biome/world setup, spawn mobs/setblock strategy. RCON is the reset permission foundation for that chain.

Audit query:

```sql
select payload
from trajectory_events
where event_type = 'environment_reset'
order by id desc
limit 1;
```

Focused tests:

```bash
backend/.venv/bin/python -m pytest \
  backend/tests/unit/test_week10_catalog_and_similarity.py \
  backend/tests/unit/test_week10_live_training.py \
  backend/tests/unit/test_skill_library.py
```

Prompt/action contract smoke:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/dump_agent_prompt.py \
  --task-id minedojo_harvest_oak_log \
  --pretty
```

Full validation:

```bash
make ci
```

## Agent POV Recording

The live training script can record an agent-focused demo video without requiring manual QuickTime/OBS operation. This is a client-side capture path: keep one Minecraft client connected to the same server, then let the script use RCON to switch that client into spectator mode and follow the first bot. The script starts and stops `ffmpeg` automatically and writes recording metadata into the live run JSON under `recording`.

Example:

```bash
export MINECRAFT_RCON_PASSWORD=<your_password>

PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
  --port <SERVER_PORT> \
  --task-id harvest_1_glass_plains_with_furnace_and_fuel \
  --worker-concurrency 1 \
  --max-steps-per-task 30 \
  --max-runtime-sec-per-task 600 \
  --clear-all-inventory-on-reset \
  --rcon-reset \
  --rcon-port <RCON_PORT> \
  --record-agent-video \
  --recording-input "4:none" \
  --recording-window-title Minecraft \
  --spectator-player <YOUR_CLIENT_PLAYER_NAME> \
  --recording-output runs/demo/agent_pov.mp4 \
  --auto-promote
```

`--spectator-player` is independent from recording: it can switch a client into spectator mode and follow the first bot through RCON without starting `ffmpeg`. If it is omitted, recording captures the visible screen but does not move the client camera. On macOS the default capture input is `Capture screen 0:none`; if device-name capture fails, list devices and pass the screen index explicitly, for example `--recording-input "4:none"`:

Spectator attachment now waits for the committed `run_started` event, which is emitted only after reset has completed. For each run the harness detaches any stale camera, restores spectator mode, teleports the client beside the bot, waits for client chunk/entity synchronization, and only then sends `/spectate`. This prevents a large reset teleport from leaving the server camera attached while the client renders the old position. Tune the synchronization window with `--spectator-chunk-sync-delay-sec` (default `0.75`) and the keepalive with `--spectator-rebind-interval-sec` (default `10`). Every command result is persisted immediately as `spectator_follow_attempt`; an interrupted run is finalized as `cancelled` instead of remaining `running`.

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

`--recording-window-title` asks AppleScript to find a visible window and generates a crop filter automatically. If macOS does not expose the Minecraft Java window, bring the client to the current desktop, leave full-screen mode, or pass a manual crop:

```bash
--recording-filter "crop=<width>:<height>:<x>:<y>"
```

## Bounded Real-Time Combat

Week 10 combat tasks need real-time reaction, but the model should not drive the fight one tick at a time. The current contract exposes three model-visible primitive actions:

- `scan_entities`: scans target entities and returns distance, height delta, line of sight, airborne estimate, melee reachability, and suggested modes.
- `move_to_and_engage_combat`: approaches and dynamically tracks the selected entity during one bounded engagement using currently equipped gear. The model still chooses melee/ranged mode, equipment, and whether to continue.
- `consume_item`: lets the model decide when to recover after `low_health` or degraded long-run state.

`engage_combat` and `fight_entity` remain hidden compatibility aliases for older manifests and trajectories. The new action neither equips nor heals automatically. Serial threat-freeze remains an observation concern and is independent from bounded automatic tracking. Combat skills remain contextual strategies rather than fixed replay macros.

When a task verifier names an explicit entity target, skill candidate construction filters mismatched `scan_entities`, `move_to_and_engage_combat`, legacy `engage_combat`, and `fight_entity` steps before policy evaluation and summarization. Filtered steps remain auditable under `source_evidence.excluded_source_steps`. Item verifiers do not enable this filter, so prerequisite combat such as killing a chicken to collect a feather is retained.

## Evidence-Gated Failure Learning

Week 10 no longer treats a failure-count threshold as permission to create a skill. Failure data is separated into raw trajectories, scoped `LearningCandidate` hypotheses, and verifier-backed `SkillSpec` records.

The lifecycle is `observed -> hypothesized -> corroborated -> validated -> promoted`. `task_timeout` alone is not evidence. A `timeout_no_progress` navigation result is durable only after at least two attempts near the same static target (within 2.5 blocks), each lasting at least 8 seconds, changing distance by no more than 0.5 blocks, and carrying pathfinder diagnostics. Moving targets, one-off stalls, model/runtime/reset failures, one-off `target_not_found`, and verifier failures without a durable diagnosis remain audit-only.

Knowledge calls provide cited hypothesis evidence but cannot validate a candidate. Validation requires a later same-scope successful run with a compatible recovery action family and a successful task verifier. World facts remain knowledge references; the skill stores the demonstrated procedure. For example, an entity document may describe Enderman teleportation, while the learned procedure is to reacquire after `target_lost` rather than chase a stale coordinate.

At batch start the runner freezes both promoted skills and active learning candidates. After every worker stops, it records failed-run hypotheses, pairs successful recoveries, and only then runs skill policy, deduplication, and promotion. Candidate signatures use `scope_key + action_type + failure_status`, are quantity-independent, and have a SQL uniqueness constraint plus row locking.

Only exact-scope `hypothesized`, `corroborated`, or `validated` candidates can appear in the next batch context. They carry `scoped_hypothesis_not_authoritative_instruction` semantics; `observed` candidates stay hidden, and promoted lessons are retrieved through the normal skill library.

Lifecycle events are `learning_candidate_skipped`, `learning_candidate_created`, `learning_candidate_updated`, `learning_candidate_validated`, and `learning_candidates_promoted`. Review candidates through `/api/learning-candidates`, and inspect per-step exposure through `context_built.retrieved_learning_candidates` in replay data.

The current implementation uses a constrained deterministic summarizer over successful trajectory evidence, cited knowledge references, and verifier-backed recovery. A later LLM reviewer may draft richer prose, but it will not bypass source-evidence, deduplication, or verifier gates.

## Current Limits

- Full catalog tasks are catalog-only and cannot all run through the deterministic benchmark runner.
- Live training currently runs executable harness manifests, not arbitrary catalog-only records.
- The full catalog does not yet include MineDojo's expanded low-level simulator specs in each local record.
- Similarity is deterministic lexical/set similarity, not embedding-based semantic similarity.
- Dedup detects duplicate skill candidates and blocks auto-promotion for near-duplicates, but merge policy is not automated yet.
- Failure learning currently extracts the last durable failure per run rather than attempting multi-segment causal attribution.
- An LLM reviewer does not yet write final skill prose; deterministic evidence-backed summaries are the current default.
