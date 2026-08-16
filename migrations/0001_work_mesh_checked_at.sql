-- 0001 — track when a work was last checked for MeSH terms.
--
-- TOUCHES A SERVING TABLE. Reconcile this into the docs repo's
-- data/database/ddl/03-publications.sql and tables/work.md.
--
-- Required by the MeSH re-check sweep (docs/prerequisites/03-publications.md).
-- NLM assigns MeSH months after publication, so a work ingested at publication
-- will never get MeSH unless something looks again. Without this column the
-- sweep cannot distinguish "PubMed has no MeSH for this yet" from "we have
-- never asked" — and the newest papers, often the most relevant for judging
-- current expertise, stay permanently unclassified. Silently.
ALTER TABLE work ADD COLUMN IF NOT EXISTS mesh_checked_at timestamptz;

-- The sweep's query shape: PMID-carrying, still MeSH-less, not checked recently.
-- Partial, because works that already have MeSH are never candidates.
CREATE INDEX IF NOT EXISTS work_mesh_recheck
    ON work (mesh_checked_at NULLS FIRST)
    WHERE pmid IS NOT NULL AND jsonb_array_length(mesh_terms) = 0;

COMMENT ON COLUMN work.mesh_checked_at IS
    'When PubMed was last asked for this work''s MeSH terms. NULL = never asked.';
