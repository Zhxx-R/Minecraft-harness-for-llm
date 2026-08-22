.PHONY: build-knowledge ci dev-backend dev-worker dev-frontend docker-up docker-down import-history-audits import-week11-creative migrate-db minecraft-pool-up minecraft-pool-down mineclip-scorer-setup mineclip-scorer-start mineclip-scorer-status mineclip-scorer-smoke mineclip-scorer-stop seed-knowledge test-python validate-schemas week10-formal-100 week11-local-creative

PYTHON ?= backend/.venv/bin/python
BACKEND_PYTHON ?= .venv/bin/python
HISTORY_IMPORT_ARGS ?=

dev-backend:
	./scripts/dev-backend.sh

dev-worker:
	./scripts/dev-worker.sh

dev-frontend:
	./scripts/dev-frontend.sh

docker-up:
	docker compose up -d postgres redis

docker-down:
	docker compose down

minecraft-pool-up:
	PYTHONPATH=backend/src $(PYTHON) scripts/start_minecraft_server_pool.py --server-count 2 --heap-gb 2.5

minecraft-pool-down:
	PYTHONPATH=backend/src $(PYTHON) scripts/stop_minecraft_server_pool.py

week10-formal-100:
	PYTHONPATH=backend/src $(PYTHON) scripts/run_week10_formal_batch.py --task-count 100 --worker-concurrency 2 --max-task-retries 5

import-week11-creative:
	PYTHONPATH=backend/src $(PYTHON) scripts/import_minedojo_creative_catalog.py

import-history-audits:
	PYTHONPATH=backend/src $(PYTHON) scripts/import_historical_audits.py --runs-root runs $(HISTORY_IMPORT_ARGS)

mineclip-scorer-setup:
	./scripts/setup_mineclip_scorer.sh

mineclip-scorer-start:
	./scripts/mineclip_scorer.sh start

mineclip-scorer-status:
	./scripts/mineclip_scorer.sh status

mineclip-scorer-smoke:
	./scripts/mineclip_scorer.sh smoke

mineclip-scorer-stop:
	./scripts/mineclip_scorer.sh stop

week11-local-creative:
	./scripts/run_week11_local_creative.sh

migrate-db:
	PYTHONPATH=backend/src $(PYTHON) -m alembic upgrade head

seed-knowledge:
	PYTHONPATH=backend/src $(PYTHON) scripts/seed_knowledge_chunks.py

test-python:
	cd backend && $(BACKEND_PYTHON) -m pytest

validate-schemas:
	$(PYTHON) scripts/validate_json_schemas.py

build-knowledge:
	node scripts/build_minecraft_knowledge.mjs

ci: validate-schemas
	$(PYTHON) -m compileall -q backend/src
	cd backend && $(BACKEND_PYTHON) -m pytest
	cd workers/mineflayer-worker && npm run typecheck
	cd frontend && npm run typecheck
