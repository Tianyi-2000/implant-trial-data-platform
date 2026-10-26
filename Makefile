# Everything runs against the local Compose database (demo credentials only).
COMPOSE    := docker compose
PSQL_ADMIN := $(COMPOSE) exec -T db psql -U itdp_admin -d itdp -v ON_ERROR_STOP=1 -q
SCHEMA_SQL := db/05_database.sql db/10_audit.sql db/20_schema.sql db/30_grants.sql db/40_load.sql db/50_corrections.sql
INGEST     := uv run python -m itdp.ingest

.PHONY: up down db reset seed load planted correct test batches queries psql

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

load:   ## master data, then every fitting export; run it twice and the second run changes nothing
	$(INGEST) master raw
	$(INGEST) exports raw/fitting/measurements

planted: ## the five problem files, A to E
	$(INGEST) exports raw/planted

correct: ## a data manager closes the open queries and locks a visit; then the audit trail (after load, planted)
	@$(COMPOSE) exec -T db psql -U data_manager_1 -d itdp -v ON_ERROR_STOP=1 < demo/corrections.sql

test:   ## pytest on a throwaway database, itdp_test, built and loaded from scratch (needs make up)
	uv run pytest

batches: ## file receipts and their reconciliation counts, by status
	@$(PSQL_ADMIN) -c "SELECT status, count(*) AS files, sum(row_count_expected) AS rows_in_headers, \
	  sum(rows_inserted) AS inserted, sum(rows_duplicate) AS duplicate, sum(rows_conflict) AS conflict, \
	  sum(rows_rejected) AS rejected FROM ops.ingest_batch GROUP BY status ORDER BY status"

queries: ## open data queries: what the system refused to decide on its own
	@$(PSQL_ADMIN) -c "SELECT query_id, query_type, issue FROM ops.data_query WHERE status <> 'closed' ORDER BY query_id"

psql:   ## interactive shell as the bootstrap superuser
	$(COMPOSE) exec db psql -U itdp_admin -d itdp
