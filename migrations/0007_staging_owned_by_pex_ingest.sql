-- 0007 — hand the staging schema's tables to the ingestion role.
--
-- Migrations run as the schema owner (0001 alters a serving table), so every
-- staging table they create is owned by that superuser. But the loaders
-- legitimately need owner rights on staging: the author-pool load drops and
-- recreates its name_key index around an 11M-row COPY, and DROP INDEX is
-- owner-only. Staging is ingestion scaffolding (migrations/README.md), so the
-- ingestion role owning it is the intended shape; `public` stays with the
-- schema owner. Idempotent, and re-run by hand after any later staging migration.
DO $$
DECLARE t record;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'staging' LOOP
        EXECUTE format('ALTER TABLE staging.%I OWNER TO pex_ingest', t.tablename);
    END LOOP;
END
$$;
