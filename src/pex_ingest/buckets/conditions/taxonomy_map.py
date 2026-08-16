"""Derive `condition_taxonomy` from who actually publishes on each condition.

**This is the inverted mapping**, and it is the substantive change national scope
forced on the runbook (docs/prerequisites/05-condition-vocabulary.md):

  "`branch_config.yaml` no longer works as specified. Its purpose was to map each
   launch specialty to its MeSH branches by hand. 'All specialties' makes that
   unwritable… The mapping has to invert. Instead of specialty → branches →
   conditions, derive `condition_taxonomy` from **which specialties' matched
   physicians actually publish on each descriptor**."

So the question "which specialties treat this condition?" is answered from
evidence rather than authored: for every condition, look at the physicians whose
papers carry its MeSH descriptors, and count their NPPES specialties. A specialty
that accounts for a meaningful share of the literature on a condition treats it;
one that appears twice by coincidence does not.

The thresholds are in `branch_config.yaml` under `specialty_mapping`, because
they are judgement calls that will need tuning once there is real data to look
at. The `min_share` guard is what keeps a huge generalist specialty — internal
medicine publishes on nearly everything — from being attached to every condition
in the vocabulary.
"""

from __future__ import annotations

import logging

import psycopg

from ...db import analyze, upsert

log = logging.getLogger("pex.conditions.taxonomy")

# Papers per (condition, taxonomy code), counted through accepted authorships.
# Only accepted matches count: a held match is one we are not confident about,
# and letting it vote here would launder that uncertainty into product data.
EVIDENCE = """
WITH condition_papers AS (
    SELECT DISTINCT cm.condition_id, e.openalex_work_id
    FROM condition_mesh cm
    JOIN staging.work_mesh_expanded e ON e.mesh_id = cm.mesh_id
),
authors AS (
    SELECT DISTINCT cp.condition_id, cp.openalex_work_id, a.npi
    FROM condition_papers cp
    JOIN authorship a ON a.openalex_work_id = cp.openalex_work_id
),
by_specialty AS (
    SELECT
        au.condition_id,
        pt.taxonomy_code,
        count(DISTINCT au.openalex_work_id) AS n_papers
    FROM authors au
    JOIN physician_taxonomy pt ON pt.npi = au.npi
    JOIN taxonomy t ON t.code = pt.taxonomy_code AND t.is_physician
    GROUP BY 1, 2
),
totals AS (
    SELECT condition_id, sum(n_papers) AS total_papers FROM by_specialty GROUP BY 1
)
SELECT
    b.condition_id,
    b.taxonomy_code,
    b.n_papers,
    round(b.n_papers::numeric / greatest(t.total_papers, 1), 4) AS share,
    row_number() OVER (PARTITION BY b.condition_id ORDER BY b.n_papers DESC) AS rank
FROM by_specialty b
JOIN totals t USING (condition_id)
WHERE b.n_papers >= %(min_papers)s
  AND b.n_papers::numeric / greatest(t.total_papers, 1) >= %(min_share)s
ORDER BY b.condition_id, b.n_papers DESC
"""

UPSERT = """
INSERT INTO condition_taxonomy (condition_id, taxonomy_code, is_primary)
VALUES (%(condition_id)s, %(taxonomy_code)s, %(is_primary)s)
ON CONFLICT (condition_id, taxonomy_code) DO UPDATE SET is_primary = EXCLUDED.is_primary
"""


def run(conn: psycopg.Connection, config) -> dict:
    settings = config.specialty_mapping or {}
    params = {
        "min_papers": int(settings.get("min_papers_per_specialty", 5)),
        "min_share": float(settings.get("min_share", 0.10)),
    }
    primary_share = float(settings.get("primary_share", 0.40))
    max_per_condition = int(settings.get("max_specialties_per_condition", 6))

    with conn.cursor() as cur:
        cur.execute(EVIDENCE, params)
        rows = cur.fetchall()

    mapped: list[dict] = []
    conditions_covered = set()
    for condition_id, taxonomy_code, _n_papers, share, rank in rows:
        if rank > max_per_condition:
            continue
        mapped.append({
            "condition_id": condition_id,
            "taxonomy_code": taxonomy_code,
            # Primary when a specialty dominates the literature *and* leads it.
            "is_primary": bool(rank == 1 and float(share) >= primary_share),
        })
        conditions_covered.add(condition_id)

    written = upsert(conn, UPSERT, mapped)
    analyze(conn, "condition_taxonomy")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM condition")
        total_conditions = cur.fetchone()[0]

    uncovered = total_conditions - len(conditions_covered)
    log.info(
        "condition_taxonomy: %s mappings across %s of %s conditions (%s unmapped)",
        f"{written:,}", f"{len(conditions_covered):,}", f"{total_conditions:,}",
        f"{uncovered:,}",
    )
    if total_conditions and uncovered / total_conditions > 0.5:
        # Expected while the works corpus is thin; a real problem once it isn't,
        # since an unmapped condition can't be reached by specialty browsing.
        log.warning(
            "%.0f%% of conditions have no specialty mapping — either the works "
            "corpus is still incomplete, or min_share is set too high",
            100 * uncovered / total_conditions,
        )
    return {"mappings": written, "conditions_covered": len(conditions_covered),
            "conditions_unmapped": uncovered}
