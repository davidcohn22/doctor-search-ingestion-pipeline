-- 0002 — OpenAlex reference sets, loaded before matching.
--
-- The matcher resolves an author's affiliation institution IDs to a US state
-- (its geography signal) and its topic IDs to a research subfield (its specialty
-- signal). Neither is answerable without these lookups, and a matcher missing
-- both degrades silently to scoring on names alone — which is precisely the
-- failure mode the whole design is built to avoid.

CREATE TABLE IF NOT EXISTS staging.openalex_institution (
    openalex_institution_id text PRIMARY KEY,     -- 'I27837315'
    display_name            text,
    country_code            text,
    city                    text,
    region                  text,                 -- as OpenAlex gives it: 'Ohio'
    state_code              text,                 -- pre-resolved to 'OH'
    type                    text,
    loaded_at               timestamptz NOT NULL DEFAULT now()
);

-- The matcher loads every US institution into memory in one query.
CREATE INDEX IF NOT EXISTS openalex_institution_state
    ON staging.openalex_institution (state_code)
    WHERE state_code IS NOT NULL;

CREATE TABLE IF NOT EXISTS staging.openalex_topic (
    openalex_topic_id text PRIMARY KEY,           -- 'T10906'
    display_name      text,
    subfield_id       text,                       -- '2705' — joins to our specialty table
    subfield_name     text,
    field_name        text,
    domain_name       text,
    loaded_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS staging.openalex_source (
    openalex_source_id text PRIMARY KEY,          -- 'S4306525036' — the journal
    display_name       text,
    issn_l             text,
    type               text,
    host_organization  text,
    loaded_at          timestamptz NOT NULL DEFAULT now()
);
