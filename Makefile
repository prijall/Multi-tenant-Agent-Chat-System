.PHONY: setup install dev api web test lint migrate seed eval

setup: install migrate

install:
	uv sync
	npm install

dev:
	$(MAKE) -j2 api web

api:
	cd backend && uv run uvicorn app.main:app --reload --port 8000

web:
	npm run dev

test:
	uv run pytest -q

lint:
	uv run ruff check .
	npm run lint

migrate:
	cd backend && uv run python -m app.scripts.bootstrap_database
	cd backend && uv run python -m app.scripts.migrate_auth_profile

seed:
	cd backend && uv run python -m app.scripts.seed

eval:
	cd backend && uv run python -m evals.runner
