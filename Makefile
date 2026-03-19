.PHONY: install test lint run migrate db db-test

install:
	pip install -e ".[dev]"

test:
	pytest -x -q

test-cov:
	pytest --cov=src --cov-report=term-missing

lint:
	ruff check src/ tests/

format:
	ruff format src/ tests/

run:
	uvicorn src.api.main:app --reload --port 8000

db:
	docker compose up -d db

db-test:
	docker compose up -d db-test

migrate:
	alembic upgrade head
