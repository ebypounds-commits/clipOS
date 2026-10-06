.PHONY: setup test lint format typecheck openapi quality devstack infra-up infra-health infra-down validate governance

setup:
	python3 -m venv .venv && .venv/bin/pip install -e 'services/api[dev]'

test:
	python3 -m pytest -q services/api

lint:
	python3 -m ruff check services/api

format:
	python3 -m ruff format services/api

typecheck:
	npx --yes -p typescript@5.5 -p next@14.2 -p react@18.3 -p react-dom@18.3 -p '@types/react@18.3' -p '@types/node@20' tsc --noEmit -p apps/web

openapi:
	PYTHONPATH=services/api python3 -c "import json; from clipos_api.main import app; spec = app.openapi(); open('openapi.json', 'w').write(json.dumps(spec, indent=2)); assert '/health' in spec['paths'], 'missing /health path'"

quality:
	python3 -m ruff format --check services/api
	python3 -m ruff check services/api
	python3 -m mypy services/api/clipos_api
	python3 -m pytest -q services/api
	bash scripts/dependency_audit.sh
	if command -v npm >/dev/null 2>&1; then npm --prefix apps/web audit --audit-level=high; else echo "SKIP npm audit: npm unavailable"; fi
	$(MAKE) openapi

devstack:
	docker compose -f docker-compose.dev.yml up -d postgres redis minio

infra-up:
	docker compose -f docker-compose.dev.yml up -d --wait || docker compose -f docker-compose.dev.yml up -d
	bash scripts/infrastructure_health.sh

infra-health:
	bash scripts/infrastructure_health.sh

infra-down:
	docker compose -f docker-compose.dev.yml down

validate:
	python3 scripts/validate_handoff.py
	bash scripts/secret_scan.sh
	$(MAKE) test

governance:
	python3 scripts/validate_state.py
	python3 scripts/architecture_guard.py
	python3 -m pytest -q services/api/tests/test_governance.py
	python3 scripts/validate_packet.py .agent/CODEX_TASK_PACKET_TEMPLATE.md
	python3 scripts/state_audit.py verify
