-- 0008 — full ORCID records for the attribution checks (2026-10-11).
--
-- orcid_employment_cache (0006) keeps only the US states an ORCID's employers sit
-- in, which is enough to confirm a match but not to reject one or to split a
-- merged profile. The ORCID check job fetches each record once and keeps what the
-- three attribution fixes need:
--
--   * affiliations — employers, education and qualifications with their country,
--     so a profile whose owner has only ever worked abroad can be rejected;
--   * dois / pmids — the owner's own works list, which the per-paper filter uses
--     to keep papers it would otherwise doubt;
--   * the owner's name, for spot-checking.
--
-- http_status records 404 (deleted) and 409 (locked) ORCIDs so they are not
-- re-requested on every run.
CREATE TABLE IF NOT EXISTS staging.orcid_record (
    orcid         text PRIMARY KEY,
    http_status   smallint NOT NULL,
    given_names   text,
    family_name   text,
    affiliations  jsonb NOT NULL DEFAULT '[]'::jsonb,
    dois          jsonb NOT NULL DEFAULT '[]'::jsonb,
    pmids         jsonb NOT NULL DEFAULT '[]'::jsonb,
    fetched_at    timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE staging.orcid_record OWNER TO pex_ingest;
