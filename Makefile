.PHONY: install dev test lint migrate seed demo build up down

install:
	cd backend && uv sync
	cd frontend && npm ci --legacy-peer-deps

dev:
	cd backend && uv run uvicorn app.main:app --reload --port 8000

test:
	cd backend && uv run pytest -q

lint:
	cd backend && uv run ruff check app tests

migrate:
	cd backend && uv run alembic upgrade head

seed:
	cd backend && uv run python scripts/seed.py

demo: seed

build:
	cd frontend && npm run build

up:
	docker compose up --build

down:
	docker compose down
