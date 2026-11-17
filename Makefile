# aftershock: `make waves` starts everything.
# Every target is a thin wrapper around docker compose, so nothing here is
# required: the README lists the raw commands for machines without make.

COMPOSE ?= docker compose
DB_USER ?= aftershock
DB_NAME ?= aftershock
FROM    ?= 2025-01-01

.DEFAULT_GOAL := help
.PHONY: help waves up down reset test logs status backfill shell psql

help: ## Show available commands
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  make %-10s %s\n", $$1, $$2}'

waves: ## Build and start everything (db, redis, migrate, web, worker, beat)
	$(COMPOSE) up --build

up: ## Same as waves, in the background
	$(COMPOSE) up --build -d

down: ## Stop everything (keeps the database)
	$(COMPOSE) down

reset: ## Stop everything and DELETE the database volume: a clean first run
	$(COMPOSE) down -v

test: ## Run the test suite inside the container
	$(COMPOSE) run --rm web pytest

logs: ## Follow worker and beat logs (ingestion activity)
	$(COMPOSE) logs -f worker beat

status: ## Show backfill progress and the sync watermark
	@$(COMPOSE) exec db psql -U $(DB_USER) -d $(DB_NAME) -c \
		"SELECT status, count(*) AS windows, sum(fetched) AS events FROM ingest_ingestwindow GROUP BY status ORDER BY status;"
	@$(COMPOSE) exec db psql -U $(DB_USER) -d $(DB_NAME) -c \
		"SELECT source, watermark, last_success_at, last_error FROM ingest_syncstate;"

backfill: ## Queue more history: make backfill FROM=2025-01-01
	$(COMPOSE) run --rm web python manage.py backfill --from $(FROM)

shell: ## Django shell inside the container
	$(COMPOSE) run --rm web python manage.py shell

psql: ## Postgres shell
	$(COMPOSE) exec db psql -U $(DB_USER) -d $(DB_NAME)