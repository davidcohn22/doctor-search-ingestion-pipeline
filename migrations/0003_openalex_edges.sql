-- 0003 — the durable authorship edge list.
--
-- Written for EVERY US-affiliated author during the one works pass, not just
-- currently-matched ones (decision 0001, "The extra win"). A few GB.
--
-- The payoff: when the matcher improves and newly matches 20,000 physicians,
-- their works are already local — no new download, no API spend. Without it,
-- every matcher improvement re-opens the question of how to fetch works for the
-- newly matched, which is the exact problem the snapshot was adopted to solve.

CREATE TABLE IF NOT EXISTS staging.openalex_authorship_edge (
    openalex_author_id text NOT NULL,
    openalex_work_id   text NOT NULL,
    author_position    text NOT NULL,
    PRIMARY KEY (openalex_author_id, openalex_work_id)
);

-- The backfill join runs author-first, which the primary key already serves.
-- This one serves the other direction: "which authors are on this work".
CREATE INDEX IF NOT EXISTS openalex_authorship_edge_work
    ON staging.openalex_authorship_edge (openalex_work_id);
