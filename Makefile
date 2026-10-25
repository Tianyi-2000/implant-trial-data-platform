# Everything runs against the local Compose database (demo credentials only).
COMPOSE    := docker compose
PSQL_ADMIN := $(COMPOSE) exec -T db psql -U itdp_admin -d itdp -v ON_ERROR_STOP=1 -q
SCHEMA_SQL := db/05_database.sql db/10_audit.sql db/20_schema.sql db/30_grants.sql

.PHONY: up down db reset seed psql

up:     ## start PostgreSQL 17 and wait until it is healthy
	$(COMPOSE) up -d --wait

down:   ## stop the database and delete its volume
	$(COMPOSE) down -v

db:     ## roles (once per cluster), then schemas, audit, tables and grants
	$(PSQL_ADMIN) < db/00_roles.sql
	@for f in $(SCHEMA_SQL); do echo "-- $$f"; $(PSQL_ADMIN) < $$f || exit 1; done

reset: down up db

seed:   ## write the synthetic trial into raw/ (same seed, same bytes)
	uv run python -m itdp.generate --out raw

psql:   ## interactive shell as the bootstrap superuser
	$(COMPOSE) exec db psql -U itdp_admin -d itdp
