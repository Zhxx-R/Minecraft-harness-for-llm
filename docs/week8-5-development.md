# Week 8.5 Development Document: Audit Replay And Evidence Chain

## Scope

Week 8.5 upgrades the Week 8 dashboard from raw audit-table browsing into a step-centric replay view. It does not change the execution loop, database schema, worker, or skill promotion logic.

The goal is to make one real run explainable in this order:

```text
Observation -> Context -> Model -> Action -> Runtime Result -> Errors
```

The raw timeline remains available for full auditability.

## Backend

New endpoint:

```http
GET /api/runs/{run_id}/replay
```

Implemented in `backend/src/mc_agent_harness/api/routes/dashboard.py`.

The endpoint reads:

- `runs`
- `trajectory_events`
- `steps`
- `model_calls`
- `runtime_errors`

It groups records by `step_index` and returns:

- run metadata
- run-level events such as `run_started` or reset-stage errors
- one `ReplayStepView` per observed step
- summary counts

Each replay step contains:

- `observation`
- `context`
- `resolved_terms`
- `retrieved_docs`
- `retrieved_skills`
- model repair/model action events
- specialized `model_calls`
- parsed action
- action result
- runtime errors
- short highlights
- raw events

## Frontend

The dashboard now has a default `Replay` tab before the existing raw views.

Implemented in:

- `frontend/src/api/client.ts`
- `frontend/src/app/App.tsx`
- `frontend/src/shared/styles.css`

The `Replay` tab shows:

- one card per step
- step status badge
- compact highlights
- six evidence blocks: Observation, Context, Model, Action, Result, Errors
- JSON drill-down for every block
- raw step events drill-down

Existing tabs remain:

- `Timeline`
- `Model Calls`
- `Runtime Errors`

## Verification

Focused validation:

```bash
backend/.venv/bin/python -m pytest backend/tests/unit/test_dashboard_api.py
cd frontend && npm run typecheck
```

Full validation:

```bash
make ci
```

Manual API check after a persisted run:

```bash
curl http://127.0.0.1:8000/api/runs/<RUN_ID>/replay
```

Manual UI check:

1. Start backend and frontend.
2. Run `scripts/demo_week3_agent.py --persist-db`.
3. Open `http://127.0.0.1:5173`.
4. Select the run.
5. Open the `Replay` tab.

## Design Notes

- The replay API is a read-only aggregation view.
- It intentionally preserves raw events so the UI never replaces the audit source.
- It is compatible with future Week 9 parallel training because it uses `run_id` and `step_index` only.
- It is compatible with future Week 10 skill review because skill source runs can be replayed through the same endpoint.
