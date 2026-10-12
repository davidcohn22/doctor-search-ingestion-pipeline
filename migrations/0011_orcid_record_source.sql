-- 0011 — where each ORCID record came from (2026-10-11).
--
-- The public API allows 25,000 anonymous reads per day per IP, which stopped the
-- first fetch at 24,520 records. The rest come from ORCID's annual Public Data
-- File (CC0, October 2025 snapshot), so a record is either 'api' (live) or
-- 'dump_2025_10'. Verdicts treat them the same; the column says how fresh each is.
ALTER TABLE staging.orcid_record ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'api';
