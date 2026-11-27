.PHONY: install migrate admin lint test status docker deploy backup

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

# Recreate every service so .env changes reach all workers, not just one.
deploy:
	docker compose up -d --build --force-recreate

backup:
	./scripts/backup.sh
