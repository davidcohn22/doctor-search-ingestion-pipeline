-- 0014 — log searches for topics the vocabulary lacks (2026-10-15).
--
-- When a user searches for a real medical topic we have no concept for, the API no
-- longer ranks doctors on a live paper-title search; it says the topic isn't covered,
-- offers related topics we do have, and records the search here. One row per distinct
-- query with a running count, so the review is "sort by times_searched" and each
-- addition to the vocabulary is a row to mark done. No identity is stored: not an IP,
-- not a client key, not a timestamp per search.
--
-- It lives in its own schema, `app`, rather than `public`: `public` is the contract
-- between the ingestion pipeline and the API and is read-only to the API, while this is
-- the one thing the API writes (future-ideas/missed-queries.md, "separate Postgres
-- schema the API can insert into, so the API stays read-only on everything else").
-- The grant is narrow: pex_app can insert rows and update only the three columns the
-- upsert changes. pex_ingest can read it and update it, to mark a topic reviewed.
--
-- **New schema outside the docs-repo DDL — reconciled into 00-extensions-and-schemas.sql,
-- 02-conditions.sql and 08-roles-and-grants.sql in the same change.**

CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.missed_search (
    missed_search_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Normalized (lowercase, filler and typed place removed), at most 200 characters.
    query_text        text NOT NULL CHECK (query_text = lower(query_text) AND length(query_text) <= 200),
    -- The AI's clinical name for the topic ("Peyronie's disease"), when it gave one.
    topic_label       text,
    times_searched    integer NOT NULL DEFAULT 1,
    first_searched_at timestamptz NOT NULL DEFAULT now(),
    last_searched_at  timestamptz NOT NULL DEFAULT now(),
    -- Review state: new → added (now in the vocabulary) or dismissed (not worth adding).
    status            text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'added', 'dismissed')),
    CONSTRAINT missed_search_query_text_key UNIQUE (query_text)
);

-- The review list: what is still new, most-searched first.
CREATE INDEX IF NOT EXISTS missed_search_review ON app.missed_search (status, times_searched DESC);

GRANT USAGE ON SCHEMA app TO pex_app, pex_ingest;
GRANT SELECT, INSERT ON app.missed_search TO pex_app;
GRANT UPDATE (topic_label, times_searched, last_searched_at) ON app.missed_search TO pex_app;
GRANT SELECT, UPDATE ON app.missed_search TO pex_ingest;
