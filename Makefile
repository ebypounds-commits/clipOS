.PHONY: setup test lint typecheck devstack validate

setup:
	python3 -m venv .venv && .venv/bin/pip install -e 'services/api[dev]'

test:
	python3 -m pytest -q services/api

lint:
	python3 -m ruff check services/api

typecheck:
	npx --yes -p typescript@5.5 -p next@14.2 -p react@18.3 -p react-dom@18.3 -p '@types/react@18.3' -p '@types/node@20' tsc --noEmit -p apps/web

devstack:
	docker compose -f docker-compose.dev.yml up -d postgres redis minio

validate:
	python3 scripts/validate_handoff.py
	bash scripts/secret_scan.sh
	$(MAKE) test
