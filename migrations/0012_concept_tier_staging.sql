-- 0012 — scratch tables for the tiered feature build (2026-10-11).
--
-- The feature build matches every paper and trial against every concept's card
-- terms (vocabulary/corpus.py) and writes one row per hit here, then adds MeSH
-- hits in SQL and keeps each item's best tier per concept. Rebuilt from scratch
-- on every features run, so UNLOGGED: nothing here needs to survive a crash.
CREATE UNLOGGED TABLE IF NOT EXISTS staging.work_concept_hit (
    openalex_work_id text     NOT NULL,
    condition_id     bigint   NOT NULL,
    tier             smallint NOT NULL,
    name_hit         boolean  NOT NULL
);

CREATE UNLOGGED TABLE IF NOT EXISTS staging.trial_concept_hit (
    nct_id       text     NOT NULL,
    condition_id bigint   NOT NULL,
    tier         smallint NOT NULL,
    name_hit     boolean  NOT NULL
);

ALTER TABLE staging.work_concept_hit OWNER TO pex_ingest;
ALTER TABLE staging.trial_concept_hit OWNER TO pex_ingest;
