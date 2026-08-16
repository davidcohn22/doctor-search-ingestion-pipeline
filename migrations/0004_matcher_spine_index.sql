-- 0004 — the matcher's view of the physician spine.
--
-- Candidate retrieval is a lookup by normalized surname + first initial,
-- including every NPPES alternate name. `physician` cannot carry that key:
-- `search_name` is a generated column and Postgres cannot expand a JSON array
-- (other_names) inside one — noted as a TODO in the docs repo's ddl/README.md.
--
-- Both matchers (publications and trials) join through these, which is what
-- makes them share one definition of "same name" rather than drifting apart.

CREATE TABLE IF NOT EXISTS staging.physician_name_key (
    name_key text NOT NULL,        -- 'hasni|s'
    npi      text NOT NULL,
    PRIMARY KEY (name_key, npi)
);

-- The hot path: name key -> candidate NPIs. Hit once per author/mention.
CREATE INDEX IF NOT EXISTS physician_name_key_npi
    ON staging.physician_name_key (npi);

CREATE TABLE IF NOT EXISTS staging.physician_match_facts (
    npi              text PRIMARY KEY,
    first_name       text,
    middle_name      text,
    last_name        text,
    credential       text,
    practice_city    text,
    practice_state   text,
    full_name        text,         -- normalized, for string similarity
    specialty_labels jsonb,        -- our specialty buckets, from NUCC taxonomy text
    name_keys        jsonb,        -- every key this physician is findable under
    built_at         timestamptz NOT NULL DEFAULT now()
);
