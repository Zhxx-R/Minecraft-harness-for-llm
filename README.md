# Minecraft Agent Harness

Industrial scaffold for a Minecraft-playing LLM agent with harness-governed execution, task-scoped memory, skill evolution, audit logs, and a visual dashboard.

## Project Shape

- `backend/`: FastAPI harness service. Owns execution loops, model routing, task memory, skill lifecycle, evaluation, audit events, and API endpoints.
- `workers/mineflayer-worker/`: Node.js Mineflayer runtime worker. Owns the live Minecraft connection and translates validated harness actions into bot operations.
- `frontend/`: React dashboard for run monitoring, task status, trajectory replay, costs, logs, and skill promotion review.
- `packages/shared-schemas/`: JSON schemas shared across backend, worker, and UI.
- `configs/`: environment-independent configuration examples, model profiles, and base prompts.
- `knowledge/`: local Minecraft/MineDojo/Mineflayer knowledge sources and generated indexes.
- `tasks/`: imported MineDojo task manifests and curated task sets.
- `docs/`: architecture notes and ADRs.
- `infra/`: Docker, migration, and deployment assets.
- `tests/`: end-to-end test scenarios.

## Runtime Boundary

Mineflayer is the only online execution runtime exposed through the harness. MineDojo is treated as a task and evaluation provider, not as an API exposed to the agent.

The LLM never receives raw Mineflayer or MineDojo APIs by default. It sees validated primitive actions such as `scan_blocks`, `move_to`, `dig_block_at`, `wait_ticks`, `process_item`, `place_block`, `equip_item`, and `move_to_and_engage_combat`. Skills are retrieved as contextual strategy memory instead of fixed macro execution.

## Runtime Profiles

Runtime profiles describe the Minecraft environment used by a run. `configs/runtime_profiles/benchmark-minimal.yaml` is the default evaluation profile. `configs/runtime_profiles/dev-fabric-observation.yaml` is an optional Fabric setup for local observation, training convenience, pausing, and near-observer respawn debugging.

Fabric mods are never exposed as LLM tools. They must be recorded as run metadata and reported separately from benchmark-minimal results.

## MVP Model

The default model profile is `qwen3.7-plus`, configured as a multimodal model. Structured game state is the primary observation channel; visual frames are injected only when needed.

## Quickstart

For a sanitized package that another Codex instance can install and run, start with [CODEX_HANDOFF.md](CODEX_HANDOFF.md) and [the Chinese handoff guide](docs/handoff/installation.zh.md).

Create the backend virtual environment and install development dependencies:

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install --upgrade pip
backend/.venv/bin/python -m pip install -c backend/constraints-handoff.txt -e "backend[dev]"
```

Validate backend and shared schemas:

```bash
make validate-schemas
make test-python
```

Install Node dependencies and typecheck the runtime/UI:

```bash
cd workers/mineflayer-worker
npm install
npm run typecheck

cd ../../frontend
npm install
npm run typecheck
```

Start local infrastructure:

```bash
make docker-up
make migrate-db
make seed-knowledge
```

Start services in separate terminals:

```bash
./scripts/dev-backend.sh
./scripts/dev-worker.sh
./scripts/dev-frontend.sh
```

The dashboard opens on an operations portal with Quick Start, agent runtime audit, skill
review, creative-task human review, and evaluation reports. Quick Start browses all 3,141
executable MineDojo snapshots and launches only the allowlisted Week10/Week11 runners after
Minecraft, RCON, client-player, and model preflight checks. See the
[Chinese dashboard and Quick Start guide](docs/dashboard-portal-and-quick-start.zh.md).

Plan the Week 10 formal 100-task pilot without starting Minecraft or calling the model:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_formal_batch.py --dry-run
```

The conservative local live profile uses two isolated Minecraft servers and two workers:

```bash
export MINECRAFT_RCON_PASSWORD=<PASSWORD>
make minecraft-pool-up
make week10-formal-100
make minecraft-pool-down
```

Formal parallel LLM runs use PostgreSQL. Each task receives one initial attempt plus at most five retries, while skill writes remain behind the full-batch barrier. See the Week 10 documents for reset, biome alignment, wave scheduling, and audit fields.

Week 11 imports all 1,560 authentic MineDojo creative tasks and evaluates completed first-person recordings through an isolated official MineCLIP service. MineCLIP scores remain external to the agent context, and uncalibrated tasks are reported as `inconclusive` rather than failed. See the Week 11 documents for model setup, one-command live execution, threshold calibration, and dashboard evidence.

Local single-instance Week 11 setup and execution:

```bash
make mineclip-scorer-setup
make mineclip-scorer-start mineclip-scorer-smoke mineclip-scorer-stop

export QWEN_API_KEY=<KEY>
export MINECRAFT_RCON_PASSWORD=<PASSWORD>
export MC_AGENT_SPECTATOR_PLAYER=<CLIENT_PLAYER_NAME>
make week11-local-creative
```

The creative wrapper runs one server and one worker, synchronizes the visible spectator client after task reset, records the agent view, then starts MineCLIP only for offline evaluation and releases it afterward.

## Development Docs

Week 1-11 establish CI, the smallest deterministic Minecraft knowledge layer, the Mineflayer worker RPC boundary, the first single-agent observe-context-model-action loop, SQL-backed audit persistence, the first high-level Minecraft action layer, a curated MineDojo-style benchmark runner, a database-backed skill evolution MVP, an audit dashboard, a step-centric replay view, a parallel training scheduler for task-isolated exploration, the full MineDojo programmatic and creative task catalogs, diversity-aware batch planning, skill candidate deduplication, and external MineCLIP creative evaluation.

See:

- [Week 1 Development Document](docs/week1-development.md)
- [Week 1 开发文档](docs/week1-development.zh.md)
- [Week 2 Development Document](docs/week2-development.md)
- [Week 2 开发文档](docs/week2-development.zh.md)
- [Week 3 Development Document](docs/week3-development.md)
- [Week 3 开发文档](docs/week3-development.zh.md)
- [Week 4 Development Document](docs/week4-development.md)
- [Week 4 开发文档](docs/week4-development.zh.md)
- [Week 5 Development Document](docs/week5-development.md)
- [Week 5 开发文档](docs/week5-development.zh.md)
- [Week 6 Development Document](docs/week6-development.md)
- [Week 6 开发文档](docs/week6-development.zh.md)
- [Week 7 Development Document](docs/week7-development.md)
- [Week 7 开发文档](docs/week7-development.zh.md)
- [Week 8 Development Document](docs/week8-development.md)
- [Week 8 开发文档](docs/week8-development.zh.md)
- [Week 8.5 Development Document](docs/week8-5-development.md)
- [Week 8.5 开发文档](docs/week8-5-development.zh.md)
- [Week 9 Development Document](docs/week9-development.md)
- [Week 9 开发文档](docs/week9-development.zh.md)
- [Week 10 Development Document](docs/week10-development.md)
- [Week 10 开发文档](docs/week10-development.zh.md)
- [Week 11 Development Document](docs/week11-development.md)
- [Week 11 开发文档](docs/week11-development.zh.md)
- [16 Week Development Plan](docs/plans/16-week-development-plan.md)
- [16 周完整开发计划](docs/plans/16-week-development-plan.zh.md)
