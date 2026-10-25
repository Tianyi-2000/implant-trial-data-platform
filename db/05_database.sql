-- Per-database setup, as the superuser. Run before the schema files.
CREATE EXTENSION IF NOT EXISTS btree_gist;  -- for the "one active config at a time" constraint

DO $$
BEGIN
  EXECUTE format('GRANT CREATE ON DATABASE %I TO audit_owner, platform_owner', current_database());
END $$;
