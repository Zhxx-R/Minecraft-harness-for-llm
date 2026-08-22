# Minecraft Agent Harness 16 Week Development Plan

## Summary

16 weeks will turn the current scaffold into a complete engineering-oriented agent project: `qwen3.7-plus` is the default multimodal main model, Mineflayer is the only online runtime, MineDojo is the task and evaluation provider, and the harness follows `H=(E,T,C,S,L,V)` to demonstrate that structured execution, knowledge, memory, skill evolution, auditability, and multi-agent orchestration improve long-horizon Minecraft agent reliability.

Optional Fabric mods are allowed only as a development/training convenience profile for observation, pausing, respawn control, and local debugging. They are not part of the agent action API and must be recorded in run metadata so benchmark results can be separated from vanilla or minimal-runtime runs.

Phase goals:

- Week 1-4: single-agent runnable kernel, early knowledge layer, worker/model/logging/persistence.
- Week 5-8: task set, evaluation, skill evolution, MVP dashboard, interview-ready demo.
- Week 9-12: parallel training, creative/MineCLIP, long-running tasks, experiment reports.
- Week 13-16: multi-agent infrastructure, cooperation/competition scenarios, engineering hardening, final materials.

## Runtime Profiles

- `benchmark-minimal`: default reproducible evaluation profile. It keeps the Minecraft environment as close as practical to the Mineflayer runtime assumptions and does not depend on convenience mods for success.
- `dev-fabric-observation`: optional local development and training profile inspired by Voyager's Fabric setup. Candidate mods include Fabric API, Mod Menu, Complete Config, Multi Server Pause, and Better Respawn. This profile is used to make observation, manual debugging, server pausing, and near-observer respawn easier.
- Runtime profile metadata must include Minecraft version, loader/version, mod list, mod config checksum, world seed, game mode, difficulty, LAN port, and whether cheats are enabled.
- The worker and agent see the same harness action interface under every profile. Mods may change environment convenience, but they must not introduce hidden actions or APIs that bypass `ToolRegistry`.
- Experiment reports must group or filter results by runtime profile. A run produced under `dev-fabric-observation` cannot be mixed into `benchmark-minimal` success-rate claims without explicit labeling.

## Implementation Plan

### Week 1: Engineering Baseline And Minimal Knowledge

- Establish CI: Python compile/tests, TypeScript typechecks, JSON schema validation.
- Harden development environment: Docker Compose, `.env.example`, local start scripts, README quickstart.
- Add the first runtime profile config files and document that Fabric mods are optional local aids, not part of the default benchmark.
- Build minimal knowledge base: Minecraft glossary, canonical item/block/entity IDs, core recipes, Mineflayer operation notes.
- Define `KnowledgeProvider`: `resolve_terms`, `get_recipe`, `retrieve_docs`, but do not automatically inject knowledge snippets into the prompt by default.
- Acceptance: when the knowledge tool is called manually, `wooden pickaxe/log/plank/crafting table` returns canonical IDs and recipe hints.

### Week 2: Mineflayer Worker RPC

- Implement backend-to-worker WebSocket JSON-RPC: `reset/observe/act/snapshot/close`.
- Worker connects to a Minecraft server and returns position, health, food, inventory, nearby blocks/entities.
- Implement base actions: `query_inventory`, `request_visual_snapshot`.
- Record worker lifecycle events: connected, spawned, disconnected, error, timeout.
- Record runtime profile metadata on connection, including LAN port, game version if available, and the configured mod profile.
- Acceptance: without LLM calls, manual actions can connect to a server and return structured observation.

### Week 3: Single-Agent Execution Loop

- Connect `qwen3.7-plus` through `ModelRouter`, supporting JSON action output and usage recording.
- Implement `observe -> context -> model -> action -> runtime -> log`.
- Inject system prompt, task spec, observation, compact evidence, action contract, and knowledge tool contract. Term resolution, recipe hints, and document snippets are obtained only when the model actively calls a knowledge tool.
- `ToolRegistry` validates action scopes and rejects raw Mineflayer JS.
- Acceptance: the agent can run a controlled inventory query, one knowledge tool call, and a simple mining attempt with full audit traces.

### Week 4: Persistence, Knowledge Indexing, And Auditability

- Add PostgreSQL/pgvector, Alembic, and SQLAlchemy models.
- Tables: runs, steps, trajectory events, model calls, runtime errors, task memories, skills, knowledge chunks.
- Persist knowledge chunks, knowledge tool call events, and deterministic retrieval sources; pgvector is enabled for later vector/hybrid retrieval, but Week 4 does not rely on naive embedding-only RAG.
- Implement minimal checkpoint/resume.
- Acceptance: after backend restart, historical runs, logs, and knowledge retrieval sources remain queryable.

### Week 5: Minecraft Action Expansion

- Worker implements `scan_blocks`, `scan_dropped_items`, `move_to`, `dig_block_at`, `wait_ticks`, `craft_item`, `place_block`, `use_item`, `fight_entity`.
- Worker may use pathfinder/collectBlock internally, but these libraries are not exposed to the LLM.
- Add optional `dev-fabric-observation` support notes for Better Respawn and Multi Server Pause, including a policy for respawning near the observer during training/debug runs only.
- Define action timeout, failure reason, recoverable error, and unrecoverable error semantics.
- Implement simple verifiers: inventory contains, block placed, entity defeated.
- Acceptance: collecting wood, crafting planks, crafting a wooden pickaxe, and placing blocks work end to end.

### Week 6: Task Provider And Basic Evaluation

- Implement `MineDojoTaskProvider` for small Harvest, TechTree, and Combat subsets.
- Define task manifests: goal, allowed actions, verifier, success criteria, knowledge tags.
- Implement benchmark runner with fixed seed, task set, and model profile.
- Benchmark metadata must include runtime profile, mod list, seed, difficulty, and respawn policy.
- Report success, steps, duration, invalid action rate, runtime crash rate, tokens, and cost.
- Acceptance: 10 basic tasks run in batch and produce JSON/Markdown reports.

### Week 7: Skill Evolution MVP

- Extend `SkillSpec`: triggers, preconditions, action plan, validation, source run, source step range, task scope, dependencies, status, and version.
- Use Postgres as the authoritative skill library. Markdown files are generated review/export snapshots for promoted or staged skills, not the runtime source of truth.
- Implement `draft -> validated -> staged -> promoted -> deprecated`.
- Build multi-level skill indexes: exact trigger/canonical ID match, task tag match, precondition/action-scope match, dependency match, and lexical similarity fallback.
- Default policy: after the same task fails at least 3 times, a later successful trajectory can generate a skill candidate.
- Skill promotion uses a database lock; training runs read fixed skill snapshots for reproducible evaluation.
- Every skill read/write/promotion/export is recorded as a trajectory event and linked to source trajectory, verifier result, and replay status.
- Acceptance: `harvest_log` can produce `harvest_oak_log`, the promoted skill exports to Markdown for human review, and new runs can reuse the database skill snapshot through `execute_skill`.

### Week 8: MVP Dashboard And Interview Demo

- UI pages: run list, run detail, event timeline, model calls, runtime errors, skill review.
- Support real-time events through Redis Streams or WebSocket fanout.
- Add raw Mineflayer code-generation baseline in a sandbox; crashes only affect the baseline.
- Compare raw codegen vs no-skill harness vs skill-evolved harness.
- The live demo may use `dev-fabric-observation` for easier observation, pausing, and recovery, while the dashboard clearly labels the runtime profile.
- Acceptance: a live demo can show that the same model is more stable under the harness.

### Week 9: Training Scheduler And Parallel Exploration

- Implement training runner with a task queue and parallel task execution.
- Each task uses an isolated memory namespace.
- Add resource budgets: max steps, max tokens, max runtime, worker concurrency.
- Training jobs may opt into `dev-fabric-observation` for faster debugging, but every run stores profile metadata and profile-specific metrics.
- Redis owns queue/run state; Postgres owns audit/final state.
- Acceptance: 5-10 tasks can run in parallel without skill write conflicts or log corruption.

### Week 10: Programmatic Catalog, Diverse Parallel Training, And Skill Governance

- Import the full MineDojo programmatic task catalog from the official task description files.
- Keep curated executable manifests separate from the full catalog: curated manifests run in CI; the full catalog drives task selection, grouping, and later live training.
- Implement task similarity scoring from goal text, category/family, knowledge tags, allowed actions, verifier target, and MineDojo metadata.
- Add a diversity-aware batch planner so parallel workers prefer low-similarity tasks in the same training epoch.
- Add skill candidate deduplication before promotion: compare action plans, triggers, task scope, dependencies, and names to merge or review near-duplicates.
- Add `SkillCreationPolicy`, an evidence-gated `LearningCandidate` state machine, and `SkillSummarizer`: failure counts trigger scoped hypothesis review rather than direct skill creation; infrastructure and stochastic noise are rejected, knowledge calls provide citations only, and a later same-scope successful recovery must pass the verifier before entering a skill. Use deterministic evidence-backed summaries first; a later LLM reviewer may draft strategy prose but cannot bypass schema, provenance, deduplication, verifier, or replay gates.
- Add live parallel programmatic training: multiple isolated Mineflayer workers run executable programmatic manifests, verify success, write task-local failure memory, and create/promote skill candidates.
- Preserve epoch semantics: workers read a fixed skill snapshot during an epoch and skill promotion happens after candidate collection/deduplication.
- Pin the current knowledge retrieval boundary: `resolve_terms/get_recipe` use deterministic alias and canonical-ID lookup, while `retrieve_docs` uses lexical overlap. Every result records retrieval mode, source snapshot, and matched aliases so the current implementation is not mistaken for semantic RAG.
- Continue skill quality governance: replay/verifier regression tests, metrics, deprecation, and progressive disclosure.
- Acceptance: the local catalog contains 1581 programmatic tasks; parallel batches avoid highly similar tasks when possible; 2-3 live Mineflayer training workers can run programmatic tasks and update skills without duplicate promotion.

### Week 11: Creative Tasks And MineCLIP

- Add MineCLIP scorer adapter for screenshots/video frames and creative prompts.
- Add creative task manifests: prompt, frame sampling policy, score threshold, calibration examples.
- MineCLIP is an external evaluator, not the agent's self-evaluator.
- UI displays creative score, key frames, and score trend.
- Acceptance: at least 3 creative tasks can be automatically scored with calibrated thresholds.

### Week 12: Long Tasks And Context Compression

- Define long tasks: resource chain to stone/iron tools, simple shelter, or nether-prep chain.
- Implement hierarchical context: current state, recent steps, task summary, retrieved memory, skills, and knowledge tool results actively requested by the agent.
- Migrate automatic knowledge injection to agent-driven knowledge retrieval: the model calls `resolve_terms/get_recipe/retrieve_docs`; the harness enforces allowlists, budgets, source attribution, prompt-injection cleanup, and audit.
- Upgrade knowledge into multi-level indexed documents: canonical item/block/entity index, recipe dependency graph, Mineflayer API/action guide, and Minecraft Wiki section index. Keep exact/lexical retrieval as the default; add BM25 plus an optional embedding reranker only when the document corpus grows, and audit which retrieval strategy was used.
- Implement stuck detection: repeated position, repeated failed action, no verifier progress.
- Allow visual frames and reflection-driven replanning when stuck.
- Acceptance: 100+ step tasks can run, resume after interruption, and replay logs.

### Week 13: Multi-Agent Infrastructure

- Add agent identity, role, message schema, inbox/outbox.
- Use Redis Streams for asynchronous leader/collector/crafter/combatant communication.
- Define lifecycle hooks for permission checks, message validation, and shared-state locks.
- Support typed team messages; avoid free-form cross-agent context pollution.
- Acceptance: two agents can asynchronously cooperate on resource collection and crafting.

### Week 14: Multi-Agent Scenarios

- Cooperation: split gathering, crafting, and simple building.
- Competition: two agents or two teams in an isolated arena with restricted actions. The LLM does not decide every tick; it emits tactical intent, target selection, gear policy, and interrupt conditions while a fast worker-side combat controller executes chasing, aiming, attacking, shielding, retreating, and eating.
- Add real-time combat baselines: Mineflayer PVP/Pathfinder/Armor Manager/Auto Eat/Hawkeye or equivalent in-house controllers. Compare rule-based/reactive control, LLM tactical control, and a dual-thread planning-acting controller.
- Refer to the research note: [Real-Time Combat Agent Research](../research/realtime-combat-agent-research.zh.md).
- Roleplay: fixed role prompts, typed messages, shared world state.
- Evaluate multi-agent overhead: success rate, communication rounds, cost, conflicts, deadlocks/timeouts.
- Acceptance: at least one cooperation scenario and one competition or roleplay scenario are demoable.

### Week 15: Engineering Hardening And Safety

- Harden sandboxing for raw codegen baseline and future code skills.
- Implement permission scopes: runtime actions, knowledge scopes, skill write scopes.
- Add rate limits, cost caps, run cancellation, and worker health checks.
- Add OpenTelemetry for service-level observability: traces, spans, latency/error metrics, and optional OTLP export, while keeping SQL audit logs as the source of truth for agent replay and skill provenance.
- Correlate OpenTelemetry trace/span ids with `run_id` and `step_index` so production traces can jump back to dashboard replay evidence.
- Harden knowledge tools with source allowlists, snapshot checksums, retrieval budgets, returned-snippet cleanup, tool-call audit, and a regression query set to prevent untrusted or overlong retrieved text from polluting prompts.
- Harden the real-time combat controller with action-rate limits, cooldowns, health/food safety thresholds, rule-based fallback on LLM timeout, arena reset isolation, and compatibility checks for server rules.
- Pin and validate supported runtime profiles, including Fabric loader/mod versions and config checksums; fail fast when a profile does not match the declared run config.
- Add failure injection tests: worker crash, invalid LLM JSON, DB restart, Redis restart.
- Acceptance: common failures do not crash the whole service and are recoverable or explainable.

### Week 16: Final Report, Demo, And Release

- Fix experiment set: 20-40 programmatic tasks, 3-5 creative tasks, 1 long task.
- Produce final report: baseline comparison, skill learning curve, cost, crash rate, long-task trajectory, multi-agent demo.
- Complete README: architecture diagram, quickstart, core design, experiment results, project highlights, roadmap.
- Prepare interview materials: 3-minute demo, 10-minute technical talk, tradeoff/problem list.
- Acceptance: from zero start, services can run, tasks execute, UI shows traces, and reports are reproducible.

## Public Interfaces

- `GameRuntime`: `reset(task_spec)`, `observe()`, `act(action)`, `snapshot()`, `close()`.
- `KnowledgeProvider`: `resolve_terms(task_text)`, `get_recipe(item_id)`, `retrieve_docs(query, limit, scope)`.
- `KnowledgeToolDispatcher`: `dispatch(tool_call)`, executes read-only knowledge tools with allowlist, scope, budget, source citation, and audit enforcement.
- `TaskProvider`: `list_tasks()`, `load_task(task_id)`, `verify(run_state)`, `score_creative(frames, prompt)`.
- `ModelRouter`: `generate_action(messages, model_profile, response_schema)`, recording tokens, latency, cost, and vision usage.
- `RuntimeProfile`: `load(profile_id)`, `validate(connection_info)`, `record_run_metadata(run_id, profile_metadata)`.
- `SkillLibrary`: `search(query, scope)`, `get(name, version)`, `create_candidate(run_id)`, `promote(candidate_id)`, `deprecate(skill_id)`, `export_markdown(skill_id)`.
- `AgentMessageBus`: `send(message)`, `receive(agent_id)`, `ack(message_id)`.
- `TrajectoryEvent`: standardized record for model calls, action calls, runtime events, verifier results, knowledge retrieval, skill reads/writes, and agent messages.

## Test Plan

- Unit tests: action schema, term resolver, recipe lookup, tool registry, context budget, model parser, skill state machine, skill indexes, message schema.
- Contract tests: backend-worker JSON-RPC, invalid action, timeout, disconnect, worker crash, snapshot failure.
- Integration tests: wood collection, plank/pickaxe crafting, block placement, stone mining, simple combat, skill reuse.
- Runtime profile tests: declared profile metadata is persisted, profile mismatch fails fast, mod-assisted runs are labeled, and mods do not add hidden agent actions.
- Knowledge tests: Minecraft-specific terms in task goals must resolve to canonical IDs, recipes, and required tools.
- Knowledge retrieval tests: a fixed query set covers exact IDs, aliases, recipe graph lookup, wiki sections, and Mineflayer action guides; each retrieval records strategy, source, matched aliases, truncation, and checksum.
- Evaluation tests: fixed task sets comparing raw codegen, no-skill harness, skill-evolved harness, and multi-agent harness.
- Long-run tests: 100+ step tasks support checkpoint, resume, stuck detection, and context compression.
- Real-time combat tests: the controller tick loop does not depend on LLM latency; rule-based fallback keeps defending/retreating on LLM timeout; arena replay audits tactical intent, controller actions, and damage events.
- Creative tests: MineCLIP thresholds are calibrated with human examples; key frames and scores are auditable.
- Regression tests: every promoted skill stores source trajectory, verifier result, replay record, fixed snapshot metadata, and Markdown export checksum.

## Assumptions

- The project is scoped as a single-developer 16-week build; Week 8 is the interview-ready MVP and Week 16 is the complete version.
- `qwen3.7-plus` is the default main model; other models stay pluggable but are not the first 8-week focus.
- Mineflayer is the only online runtime; MineDojo supplies task metadata and evaluation.
- Fabric mods are optional development/training aids. They help observation, server pausing, and respawn control, but are not exposed to the LLM and are excluded from default benchmark claims unless explicitly labeled.
- Knowledge starts in Week 1, so the agent does not guess Minecraft internal IDs.
- Web search is disabled by default and only used later as a controlled ablation.
- Initial skills are structured action sequences stored canonically in Postgres. Markdown exports are human-review snapshots; code skills are later optimizations requiring sandboxing, replay, and verifier approval.
