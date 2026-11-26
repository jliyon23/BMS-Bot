.PHONY: install migrate admin lint test status docker

install:
	pip install -e ".[dev]"

migrate:
	alembic upgrade head

admin:
	bmsbot create-admin

lint:
	ruff check src tests

test:
	pytest -q

status:
	bmsbot status

docker:
	docker compose up --build -d
