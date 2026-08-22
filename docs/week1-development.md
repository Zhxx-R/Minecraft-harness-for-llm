# Week 1 Development Document

## Goal

Week 1 establishes the engineering baseline and the smallest useful Minecraft knowledge layer. The agent should not rely on pretrained memory to guess Minecraft terms such as `wooden pickaxe`, `log`, `plank`, or `crafting table`.

## Delivered Changes

- Saved the full 16-week implementation plan in `docs/plans/16-week-development-plan.md`.
- Added CI workflow in `.github/workflows/ci.yml`:
  - Python backend install, compile, schema validation, tests.
  - Mineflayer worker TypeScript typecheck.
  - Frontend TypeScript typecheck.
- Added `scripts/validate_json_schemas.py` for shared schema validation.
- Added `Makefile` targets:
  - `make validate-schemas`
  - `make test-python`
  - `make ci`
- Added minimal Minecraft knowledge data in `knowledge/raw/minimal_minecraft_knowledge.json`.
- Added Mineflayer operation notes in `knowledge/raw/mineflayer_operation_guide.md`.
- Added backend knowledge package:
  - `KnowledgeProvider` protocol.
  - `StaticKnowledgeProvider` deterministic file-backed implementation.
  - Dataclass models for terms, recipes, documents, and resolved terms.
- Added unit tests for term resolution, recipe lookup, and local document retrieval.

## Knowledge Design

The Week 1 provider is intentionally deterministic and small. It is not a vector database. It is a stable grounding layer that remains useful after Week 4 because canonical IDs and recipes should be resolved deterministically instead of guessed through embedding retrieval.

`StaticKnowledgeProvider.resolve_terms(task_text)`:

- Scans task text for known aliases.
- Returns canonical Minecraft IDs.
- Attaches recipe hints when the resolved term is craftable.

`StaticKnowledgeProvider.get_recipe(item_id)`:

- Returns station, ingredients, output count, required station/block, and description.

`StaticKnowledgeProvider.retrieve_docs(query, limit)`:

- Uses simple lexical overlap across local guide documents.
- Returns only local project knowledge, not web search.

## Week 1 Acceptance Example

Input:

```text
Craft a wooden pickaxe from logs, planks, and a crafting table.
```

Expected resolved terms:

- `wooden_pickaxe`: item, recipe requires `crafting_table`, ingredients `oak_planks x3`, `stick x2`.
- `oak_log`: block, starter tree resource.
- `oak_planks`: item, crafted from `oak_log`.
- `crafting_table`: block/station, crafted from `oak_planks x4`.

This behavior is covered by `backend/tests/unit/test_knowledge_provider.py`.

## Development Commands

Install backend dev dependencies:

```bash
cd backend
python -m pip install -e ".[dev]"
```

Run backend tests:

```bash
cd backend
pytest
```

Validate shared JSON schemas:

```bash
python scripts/validate_json_schemas.py
```

Run all local CI checks after Node dependencies are installed:

```bash
make ci
```

## Current Boundaries

- The knowledge layer is local-only and deterministic.
- No web search is used.
- No Mineflayer runtime behavior was changed in Week 1.
- The provider is not yet wired into `ContextManager`; that is Week 3 work after the worker RPC and model loop are available.
- SQL-backed knowledge persistence starts in Week 4. pgvector is enabled for later vector/hybrid retrieval, but deterministic term and recipe lookup remains part of the harness contract.

## Verification

Week 1 should be considered complete when these pass:

```bash
python -m compileall -q backend/src
python scripts/validate_json_schemas.py
cd backend && pytest
```

Worker and frontend typechecks are configured in CI; locally they require `npm install` in each Node project before running `npm run typecheck`.

Verified locally with:

```bash
PYTHON=/Users/zmchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 make ci
```

Result:

- Shared schema validation passed: 3 schemas.
- Backend tests passed: 4 tests.
- Worker TypeScript typecheck passed.
- Frontend TypeScript typecheck passed.

Known non-blocking warnings:

- FastAPI/Starlette emits a `TestClient` deprecation warning from upstream packages.
- `npm install` reports audit warnings in frontend and worker dependency trees. These are recorded but not fixed with `npm audit fix --force` because forced upgrades may introduce breaking changes before runtime integration exists.
