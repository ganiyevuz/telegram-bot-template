# `.env` is deliberately NOT included/exported here. `docker compose` reads it itself and
# parses it properly; make's `include` does not — it keeps the quotes around `DB_USER="tgbot"`
# (postgres then bootstraps with a literal `""tgbot""` and crash-loops) and keeps the
# whitespace before a trailing `# comment` (compose then rejects `GRAFANA_PORT=3000   ` as an
# invalid host port). No target below needs a variable from .env.

LOCALES = bot/locales

.PHONY: help

help: ## Display this help screen
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

deps:	## Install dependencies
	@uv sync --frozen
.PHONY: deps

run-polling: ## Run the bot with long polling (development only; no API, no metrics)
	uv run python -m bot
.PHONY: run-polling

compose-up: ## Run docker compose
	docker compose up --build -d
.PHONY: compose-up

compose-down: ## Down docker compose
	docker compose down
.PHONY: compose-down

compose-stop: ## docker compose stop
	docker compose stop

compose-kill: ## docker compose kill
	docker compose kill

compose-build: ## docker compose build
	docker compose build

compose-ps: ## docker compose ps
	docker compose ps

compose-exec: ## Exec command in the api container, e.g. make compose-exec args="alembic current"
	docker compose exec api $(args)

logs: ## Tail logs of one service, e.g. make logs args=api
	docker compose logs $(args) -f

logs-worker: ## Follow worker logs
	docker compose logs worker -f

logs-scheduler: ## Follow scheduler logs
	docker compose logs scheduler -f

# MIGRATIONS
mm: ## Create new migrations with args name in docker compose
	docker compose exec api alembic revision --autogenerate -m "$(args)"
.PHONY: mm

migrate: ## Upgrade migrations in docker compose
	docker compose exec api alembic upgrade head
.PHONY: migrate

downgrade: ## Downgrade to args name migration in docker compose
	docker compose exec api alembic downgrade $(args)
.PHONY: downgrade

# STYLE
check: ## Run linters to check code
	@uv run ruff check .
	@uv run ruff format --check .
.PHONY: check

format: ## Run linters to fix code
	@uv run ruff check --fix .
	@uv run ruff format .
.PHONY: format

clean: ## Delete all temporary and generated files
	@rm -rf .pytest_cache .ruff_cache .hypothesis build/ -rf dist/ .eggs/ .coverage coverage.xml coverage.json htmlcov/ .mypy_cache
	@find . -name '*.egg-info' -exec rm -rf {} +
	@find . -name '*.egg' -exec rm -f {} +
	@find . -name '*.pyc' -exec rm -f {} +
	@find . -name '*.pyo' -exec rm -f {} +
	@find . -name '*~' -exec rm -f {} +
	@find . -name '__pycache__' -exec rm -rf {} +
	@find . -name '.pytest_cache' -exec rm -rf {} +
	@find . -name '.ipynb_checkpoints' -exec rm -rf {} +
.PHONY: clean

# BACKUPS
# All three run against the `postgres` service. They used to name `api` and `app_db`:
# `api` is the Python image and has no pg_dump, and `app_db` is not a service in this
# compose file at all, so none of them had ever worked. docker-compose.yml mounts
# ./scripts/postgres at /scripts in `postgres` and gives it the same backups-data
# volume pgbackup writes to, so a manual dump lands beside the scheduled ones.
backup: ## Dumps the database to the backups volume as backup-<timestamp>.dump.gz
	docker compose exec postgres /scripts/backup
.PHONY: backup

mount-docker-backup: ## Copies one backup out of the volume to the working directory: args=<file>
	docker compose cp postgres:/backups/$(args) ./$(args)
.PHONY: mount-docker-backup

restore: ## DROPS the database and restores it from a backup in the volume: args=<file>
	docker compose exec postgres /scripts/restore $(args)
.PHONY: restore

# I18N
babel-extract: ## Extracts translatable strings from the source code into a .pot file
	@uv run pybabel extract --input-dirs=. -o $(LOCALES)/messages.pot
.PHONY: locales-extract

babel-update: ## Updates .pot files by merging changed strings into the existing .pot files
	@uv run pybabel update -d $(LOCALES) -i $(LOCALES)/messages.pot
.PHONY: locales-update

babel-compile: ## Compiles translation .po files into binary .mo files
	@uv run pybabel compile -d $(LOCALES)
.PHONY: locales-compile

babel: extract update
.PHONY: babel
