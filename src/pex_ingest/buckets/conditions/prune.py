"""Prune candidate descriptors by publication frequency.

MeSH has ~30,000 descriptors, most of which no patient will ever search for. The
filter that decides which become real conditions is "does our ingested literature
actually write about this?" — which is why this bucket finalizes after bucket 3
even though the parse can run any time.

**Two changes from the runbook, both forced by national scope.**

1. *The threshold is relative, not absolute.* The runbook's ≥25 papers was sized
   for one narrow vertical against a partial corpus. Nationally, with all works
   ingested, far more descriptors clear 25 and the filter stops filtering. So the
   primary rule is top-N within each second-level branch (`C12.294`), with the
   absolute count kept as a floor. A relative rule also self-corrects as the
   corpus grows, which an absolute one does not.

2. *The counting query expands `work.mesh_terms`.* The schema stores MeSH tags as
   a JSON array on `work`, not as a `work_mesh` link table
   (docs/database/ddl/README.md#known-inconsistency-work_mesh). Containment is
   the right shape for "does this paper cover descriptor D?" but the wrong shape
   for "how many distinct papers carry descriptor D?" across 6,000 descriptors.
   So this module materializes the expansion once into
   `staging.work_mesh_expanded` and counts against that — which keeps
   `work.mesh_terms` canonical while giving the pruning query the index it wants.
   The expansion is derived and disposable; it is rebuilt, never maintained.

Descriptors that fail the threshold simply don't become conditions. Their papers
still count toward their surviving *parent* via the child mappings, so a strict
threshold loses less than it appears to.
"""

from __future__ import annotations

import logging

import psycopg

from ...db import analyze

log = logging.getLogger("pex.conditions.prune")

# One pass over `work`, expanding each paper's MeSH array into rows.
REBUILD_EXPANSION = """
TRUNCATE staging.work_mesh_expanded;

INSERT INTO staging.work_mesh_expanded (openalex_work_id, mesh_id)
SELECT w.openalex_work_id, term
FROM work w
CROSS JOIN LATERAL jsonb_array_elements_text(w.mesh_terms) AS term
WHERE jsonb_array_length(w.mesh_terms) > 0
ON CONFLICT DO NOTHING;
"""

# Which descriptors count toward which candidate: itself, plus every descriptor
# strictly beneath it in the MeSH tree.
REBUILD_COVERAGE = """
TRUNCATE staging.mesh_coverage;

INSERT INTO staging.mesh_coverage (mesh_id, covered_mesh_id)
SELECT mesh_id, mesh_id FROM staging.mesh_candidates
UNION
SELECT mesh_id, child_mesh_id FROM staging.mesh_candidate_child
ON CONFLICT DO NOTHING;
"""

# Distinct papers per candidate. DISTINCT matters: a paper tagged with both a
# parent and two of its children must count once, not three times.
REBUILD_COUNTS = """
TRUNCATE staging.mesh_paper_count;

INSERT INTO staging.mesh_paper_count (mesh_id, n_papers, n_first_last_papers)
SELECT
    cov.mesh_id,
    count(DISTINCT e.openalex_work_id),
    count(DISTINCT e.openalex_work_id) FILTER (
        WHERE EXISTS (
            SELECT 1 FROM authorship a
            WHERE a.openalex_work_id = e.openalex_work_id
              AND a.author_position IN ('first', 'last')
        )
    )
FROM staging.mesh_coverage cov
JOIN staging.work_mesh_expanded e ON e.mesh_id = cov.covered_mesh_id
GROUP BY cov.mesh_id;
"""

# The survivors: top-N within each branch, with an absolute floor.
SURVIVORS = """
WITH ranked AS (
    SELECT
        c.mesh_id,
        c.name,
        c.kind,
        coalesce(p.n_papers, 0) AS n_papers,
        -- The branch a descriptor is ranked within: the first `depth` segments
        -- of its first tree number, e.g. 'C12.294'.
        (SELECT array_to_string((string_to_array(t.tree, '.'))[1:%(depth)s], '.')
           FROM jsonb_array_elements_text(c.tree_numbers) AS t(tree)
          LIMIT 1) AS branch
    FROM staging.mesh_candidates c
    LEFT JOIN staging.mesh_paper_count p ON p.mesh_id = c.mesh_id
),
numbered AS (
    SELECT *,
           row_number() OVER (PARTITION BY branch ORDER BY n_papers DESC, mesh_id) AS rank_in_branch
    FROM ranked
)
SELECT mesh_id, name, kind, n_papers, branch, rank_in_branch
FROM numbered
WHERE n_papers >= %(min_papers)s
  AND rank_in_branch <= %(top_n)s
ORDER BY n_papers DESC
"""


def rebuild_counts(conn: psycopg.Connection) -> dict:
    """Materialize the expansion, the coverage map, and the per-descriptor counts."""
    with conn.cursor() as cur:
        log.info("expanding work.mesh_terms (one pass over `work`)")
        cur.execute(REBUILD_EXPANSION)
        conn.commit()

        cur.execute(REBUILD_COVERAGE)
        conn.commit()

        log.info("counting papers per candidate descriptor")
        cur.execute(REBUILD_COUNTS)
        conn.commit()

        cur.execute("SELECT count(*) FROM staging.work_mesh_expanded")
        expanded = cur.fetchone()[0]
        cur.execute("SELECT count(*), sum(n_papers) FROM staging.mesh_paper_count")
        counted, total_papers = cur.fetchone()

    analyze(conn, "staging.work_mesh_expanded", "staging.mesh_paper_count",
            "staging.mesh_coverage")
    log.info(
        "%s work↔descriptor rows expanded; %s candidates have ≥1 paper",
        f"{expanded:,}", f"{counted:,}",
    )
    if expanded == 0:
        log.error(
            "no MeSH-tagged works at all — bucket 3 has not been loaded, so the "
            "prune would discard every candidate. Load publications first."
        )
    return {"expanded": expanded, "counted": counted, "total_papers": total_papers or 0}


def survivors(conn: psycopg.Connection, config) -> list[dict]:
    """The descriptors that become conditions, under the configured thresholds."""
    params = {
        "min_papers": int(config.prune.get("min_papers", 25)),
        "top_n": int(config.prune.get("top_n_per_branch", 500)),
        "depth": int(config.prune.get("branch_depth", 2)),
    }
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM staging.mesh_candidates")
        candidates = cur.fetchone()[0]
        cur.execute(SURVIVORS, params)
        rows = [
            {"mesh_id": r[0], "name": r[1], "kind": r[2], "n_papers": r[3],
             "branch": r[4], "rank_in_branch": r[5]}
            for r in cur.fetchall()
        ]

    log.info(
        "prune: %s of %s candidates survive (min_papers=%s, top_n_per_branch=%s)",
        f"{len(rows):,}", f"{candidates:,}", params["min_papers"], params["top_n"],
    )
    # "If ~everything survives, N is too low to be doing any work."
    if candidates and len(rows) / candidates > 0.95:
        log.warning(
            "%.0f%% of candidates survived — the threshold is not filtering. "
            "Raise min_papers or lower top_n_per_branch.",
            100 * len(rows) / candidates,
        )
    if candidates and len(rows) / max(candidates, 1) < 0.05:
        log.warning(
            "only %.1f%% of candidates survived — the threshold is probably too "
            "strict, or the works corpus is still thin.",
            100 * len(rows) / candidates,
        )
    return rows
