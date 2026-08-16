-- 0006 — derived tables for the condition pipeline and the ORCID cache.
--
-- work_mesh_expanded is the pragmatic resolution of the `work_mesh` open
-- question (see migrations/README.md). `work.mesh_terms` stays canonical and is
-- what the serving application reads; this is a DERIVED expansion, truncated and
-- rebuilt by the prune step, never maintained incrementally.
--
-- The reason it exists: containment on a JSON array answers "does this paper
-- cover descriptor D?" well, but "how many distinct papers carry descriptor D?"
-- across ~6,000 descriptors badly — and that is exactly the pruning query that
-- decides which conditions exist, plus the relevance join behind every row of
-- physician_condition_features.

CREATE TABLE IF NOT EXISTS staging.work_mesh_expanded (
    openalex_work_id text NOT NULL,
    mesh_id          text NOT NULL,
    PRIMARY KEY (openalex_work_id, mesh_id)
);

-- The direction that matters: descriptor -> papers.
CREATE INDEX IF NOT EXISTS work_mesh_expanded_mesh
    ON staging.work_mesh_expanded (mesh_id);

-- Which descriptors count toward which candidate: itself plus every descriptor
-- strictly beneath it in the MeSH tree. So a paper tagged with a narrower child
-- still counts toward the parent condition, which is what stops a strict pruning
-- threshold from losing as much as it appears to.
CREATE TABLE IF NOT EXISTS staging.mesh_coverage (
    mesh_id         text NOT NULL,
    covered_mesh_id text NOT NULL,
    PRIMARY KEY (mesh_id, covered_mesh_id)
);

CREATE INDEX IF NOT EXISTS mesh_coverage_covered
    ON staging.mesh_coverage (covered_mesh_id);

CREATE TABLE IF NOT EXISTS staging.mesh_paper_count (
    mesh_id             text PRIMARY KEY,
    n_papers            integer NOT NULL DEFAULT 0,
    n_first_last_papers integer NOT NULL DEFAULT 0,
    computed_at         timestamptz NOT NULL DEFAULT now()
);

-- ORCID employment lookups, cached including negatives.
-- "Cache results — the same ORCID gets checked repeatedly across matcher
-- re-runs" (docs/prerequisites/03-publications.md). The matcher is designed to
-- be re-run freely while its weights are tuned; without this, every re-run would
-- re-issue the same thousands of calls against a free public API that publishes
-- no rate limit and deserves not to be tested on it.
CREATE TABLE IF NOT EXISTS staging.orcid_employment_cache (
    orcid         text PRIMARY KEY,
    states        jsonb NOT NULL DEFAULT '[]'::jsonb,   -- two-letter US states
    organizations jsonb NOT NULL DEFAULT '[]'::jsonb,
    fetched_at    timestamptz NOT NULL DEFAULT now()
);
