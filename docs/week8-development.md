# Week 8 Development Document: MVP Dashboard And Interview Demo

## Scope

Week 8 turns the persisted audit data from Weeks 4-7 into an operator-facing MVP dashboard. The current implementation focuses on four deliverables:

- Dashboard API for runs, timeline events, model calls, runtime errors, skills, and benchmark comparison.
- React dashboard with run list, run detail tabs, skill review controls, and Week 8 comparison table.
- Raw Mineflayer codegen baseline sandbox scaffold that is isolated from the live harness path.
- Repeatable comparison report script that loads measured Week 6 benchmark output.

## Backend API

The new FastAPI route module is `backend/src/mc_agent_harness/api/routes/dashboard.py`.

Public endpoints:

- `GET /api/runs`: recent run list with step, event, model-call, and runtime-error counts.
- `GET /api/runs/{run_id}`: run metadata and task spec.
- `GET /api/runs/{run_id}/events?after_id=0`: ordered trajectory timeline.
- `GET /api/runs/{run_id}/model-calls`: persisted model outputs, parsed actions, usage, and raw response metadata.
- `GET /api/runs/{run_id}/runtime-errors`: worker/runtime failures tied to run and step.
- `GET /api/skills`: skill review rows across lifecycle states.
- `GET /api/skills/{skill_id}`: full skill JSON spec.
- `POST /api/skills/{skill_id}/promote`: dashboard review promotion.
- `POST /api/skills/{skill_id}/deprecate`: dashboard review deprecation with reason.
- `GET /api/benchmark-comparison`: Week 8 comparison assembled from local artifacts.

The dashboard API reads the SQL audit tables directly and does not change the execution loop. Promotion/deprecation keeps the SQL `skills` table as the source of truth and writes a trajectory event to the source run when that run exists.

## Frontend

The React dashboard is implemented in:

- `frontend/src/app/App.tsx`
- `frontend/src/api/client.ts`
- `frontend/src/shared/styles.css`

The UI currently polls every 3 seconds. This is the Week 8 MVP replacement for real-time observation; the endpoint shape already supports incremental event reads through `after_id`, so Redis Streams or WebSocket fanout can replace polling later without changing the page model.

Main views:

- Summary strip: run count, promoted skill count, runtime error count, refresh clock.
- Run list: persisted runs with lifecycle status and step count.
- Run audit detail: timeline, model calls, runtime errors.
- Skill review: promote/deprecate controls.
- Week 8 comparison: raw codegen baseline, no-skill harness, skill-evolved harness.

Set `VITE_API_BASE_URL=http://127.0.0.1:8000` when the frontend is served separately from the backend.

## Raw Codegen Baseline

The baseline scaffold is implemented in `backend/src/mc_agent_harness/evaluation/baselines.py`.

The current sandbox intentionally does not execute arbitrary generated Mineflayer code against the live Minecraft worker. It performs:

- Source-size limit.
- Disallowed pattern scan for high-risk Node APIs such as `child_process`, `process`, `fs`, `net`, `eval`, and dynamic `Function`.
- `node --check` syntax validation with timeout.

This gives a safe Week 8 baseline artifact. A candidate that fails policy or syntax checks counts as a raw-codegen baseline crash/failure, but it cannot crash the main harness service.

## Comparison Report

The comparison builder is implemented in `backend/src/mc_agent_harness/evaluation/comparison.py`.

The runner is:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week8_comparison.py
```

Optional raw JS baseline candidates:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week8_comparison.py \
  --raw-js path/to/generated_candidate.js
```

Outputs are written to `runs/week8/` as JSON and Markdown.

Current data quality:

- `raw_codegen_baseline`: `sandbox_ready` until raw JS candidates are supplied.
- `no_skill_harness`: measured from the latest `runs/week6/*.json`.
- `skill_evolved_harness`: `pending_replay` until a promoted-skill replay benchmark exists.

This is deliberate. The dashboard should not fabricate skill-evolved gains before replay is implemented.

## Validation

Focused validation:

```bash
backend/.venv/bin/python -m pytest backend/tests/unit/test_dashboard_api.py backend/tests/unit/test_week8_comparison.py
cd frontend && npm run typecheck
PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week8_comparison.py
```

Full project validation:

```bash
make ci
```

Local dashboard:

```bash
./scripts/dev-backend.sh
cd frontend
VITE_API_BASE_URL=http://127.0.0.1:8000 npm run dev -- --host 127.0.0.1
```

Open `http://127.0.0.1:5173`.

## Known Limits

- The UI uses polling, not Redis Streams/WebSocket fanout yet.
- Skill-evolved benchmark replay is not measured yet; the comparison row is explicitly marked `pending_replay`.
- The raw baseline sandbox checks syntax and policy only; true isolated runtime execution should be added with a container or restricted worker before allowing generated JS to run.
