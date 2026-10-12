# Stack local y verificación (F1-10). Runbook: docs/runbooks/local-stack.md.
# `make check` ejecuta las mismas verificaciones que CI (backend.yml, frontend.yml, security.yml)
# contra los servicios del compose (PostgreSQL 18.6 y Garage v2.4.1) y la BD separada de tests.
# Las pruebas de humo de las imágenes usan el mismo script que CI (infra/docker/smoke-image.sh).
# `.env` se lee con las reglas de make: usar valores alfanuméricos/hex (sin `$`, `#`, comillas).
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c  # GNU make ≥ 3.82; con 3.81 (macOS) cada cadena usa `&&`
ENV_FILE ?= infra/env/.env
COMPOSE := docker compose -f infra/docker/compose.yaml --env-file $(ENV_FILE)
-include $(ENV_FILE)

PG := 127.0.0.1:$(or $(POSTGRES_HOST_PORT),5432)/$(or $(CRM_TEST_DB_NAME),crm_test)
TEST_ENV := DATABASE_URL=postgres://crm_app:$(CRM_APP_PASSWORD)@$(PG) \
	DATABASE_MIGRATOR_URL=postgres://crm_migrator:$(CRM_MIGRATOR_PASSWORD)@$(PG) \
	STORAGE_ENDPOINT_URL=http://127.0.0.1:$(or $(GARAGE_HOST_PORT),3900) STORAGE_REGION=garage \
	STORAGE_BUCKET=$(or $(STORAGE_BUCKET),crm-local) STORAGE_ACCESS_KEY_ID=$(STORAGE_ACCESS_KEY_ID) \
	STORAGE_SECRET_ACCESS_KEY=$(STORAGE_SECRET_ACCESS_KEY) CI=1
DEPLOY_ENV := DJANGO_SETTINGS_MODULE=config.settings.production DJANGO_ALLOWED_HOSTS=app.example.com \
	FORWARDED_ALLOW_IPS=127.0.0.1 \
	DATABASE_URL=postgres://ci:ci@localhost:5432/ci DJANGO_SECRET_KEY=$$(openssl rand -hex 32) \
	SECRETS_KEKS=1:$$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')

.PHONY: up down logs check check-repo check-images check-backend check-frontend
up: ## Stack completo en http://localhost:$(or $(PROXY_HOST_PORT),8080)
	$(COMPOSE) up -d --build --wait
down:
	$(COMPOSE) down
logs:
	$(COMPOSE) logs -f

check: check-repo check-images check-backend check-frontend ## Las verificaciones de CI

check-repo:
	cd .github/scripts && python3 -m unittest -q
	GARAGE_RPC_SECRET=ci-config-only STORAGE_ACCESS_KEY_ID=ci-config-only \
		STORAGE_SECRET_ACCESS_KEY=ci-config-only docker compose -f infra/docker/compose.yaml \
		--env-file infra/env/.env.example config -q
	gitleaks git --config .gitleaks.toml --redact --no-banner --exit-code 1 .

check-images: ## Imágenes como en CI: build, usuario no root y prueba de humo HTTP
	docker build --pull -t crm-backend:ci backend
	test "$$(docker run --rm --entrypoint id crm-backend:ci -u)" = "10001"
	DJANGO_SECRET_KEY="$$(openssl rand -hex 32)" \
	SECRETS_KEKS="1:$$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')" \
		sh infra/docker/smoke-image.sh crm-backend:ci 8000 /health/live -e DJANGO_SECRET_KEY -e SECRETS_KEKS \
		-e DJANGO_ALLOWED_HOSTS=app.example.com -e FORWARDED_ALLOW_IPS=127.0.0.1 \
		-e DATABASE_URL=postgres://ci:ci@127.0.0.1:5432/ci
	docker build --pull -t crm-frontend:ci frontend
	test "$$(docker run --rm --entrypoint id crm-frontend:ci -u)" != "0"
	SMOKE_CHECK="node frontend/scripts/check-security-headers.mjs" sh infra/docker/smoke-image.sh crm-frontend:ci 3000 /o/ci

check-backend:  # `@`: la línea lleva credenciales locales (TEST_ENV); make no la imprime
	$(COMPOSE) up -d --wait postgres garage
	@echo "+ backend: ruff, mypy, import-linter, check, migraciones, OpenAPI, pip-audit, pytest"
	@tmp="$$(mktemp -d)" && cd backend && uv sync --frozen && uv run ruff format --check . && uv run ruff check . \
		&& uv run mypy . && uv run lint-imports \
		&& DJANGO_SETTINGS_MODULE=config.settings.test uv run python manage.py check \
		&& env -u DATABASE_MIGRATOR_URL $(DEPLOY_ENV) uv run python manage.py check --deploy --fail-level WARNING \
		&& $(TEST_ENV) DJANGO_SETTINGS_MODULE=config.settings.test uv run python manage.py makemigrations --check --dry-run \
		&& DJANGO_SETTINGS_MODULE=config.settings.local uv run python manage.py spectacular --validate --fail-on-warn --file $$tmp/schema.yaml \
		&& diff -u openapi/schema.yaml $$tmp/schema.yaml \
		&& uv export --frozen --all-groups --format requirements-txt -o $$tmp/requirements.txt \
		&& uv run pip-audit -r $$tmp/requirements.txt --require-hashes --disable-pip \
		&& $(TEST_ENV) uv run pytest

check-frontend:
	cd frontend && pnpm install --frozen-lockfile && pnpm format:check && pnpm lint && pnpm typecheck \
		&& pnpm test --run && pnpm api:generate && git diff --exit-code -- src/lib/api \
		&& test -z "$$(git status --porcelain -- src/lib/api)" && pnpm build \
		&& pnpm audit --prod --audit-level=high
