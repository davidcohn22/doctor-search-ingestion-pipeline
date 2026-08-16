"""Rebuild the precomputed search features.

These are the "ingredients" the ranking mixes per query. They are rebuilt as the
last step of every ingestion run, **never written at request time**, and final
scores are never stored — the product's philosophy is that the evidence is the
product and the ranking is a transparent aggregation over it, so what gets stored
are the counts a user could check, not an opaque number.

Two tables, split by what they depend on:

  * `physician_condition_features` — one row per doctor × condition, and also the
    condition-search entry point: "doctors with any BPH evidence" is simply
    "doctors with a BPH row here".
  * `physician_features` — one row per doctor, for the numbers that don't depend
    on the searched condition.

**Why counting runs through `staging.work_mesh_expanded`.** The relevance join is
"papers whose MeSH terms intersect this condition's descriptors", across every
doctor × every condition. Written against `work.mesh_terms` that is a JSON
containment test per pair; against the materialized expansion it is an ordinary
index join. The expansion is rebuilt by the prune step and is derived from
`work.mesh_terms`, which stays canonical.
"""

from __future__ import annotations

import logging

import psycopg

log = logging.getLogger("pex.features")

# How recent counts as "recent". The schema notes this is TODO and likely 5
# years; it is a constant here so changing it is one edit and one rebuild.
RECENCY_YEARS = 5

# Statuses that mean a trial is currently running. ClinicalTrials.gov's
# vocabulary is wider than "recruiting": a trial that has closed enrolment but is
# still treating patients is very much active evidence of ongoing involvement.
ACTIVE_TRIAL_STATUSES = (
    "RECRUITING",
    "ACTIVE_NOT_RECRUITING",
    "ENROLLING_BY_INVITATION",
    "NOT_YET_RECRUITING",
)

REBUILD_CONDITION_FEATURES = """
TRUNCATE physician_condition_features;

INSERT INTO physician_condition_features (
    npi, condition_id, relevant_pub_count, first_last_pub_count, recent_pub_count,
    earliest_pub_year, latest_pub_year, trial_count, active_trial_count, computed_at
)
WITH publication_evidence AS (
    -- DISTINCT on (npi, condition, work) matters: a condition maps to a primary
    -- descriptor plus its children, so one paper can match a condition several
    -- times over and would otherwise be counted several times over.
    SELECT DISTINCT
        a.npi,
        cm.condition_id,
        a.openalex_work_id,
        a.author_position,
        w.publication_year
    FROM authorship a
    JOIN work w ON w.openalex_work_id = a.openalex_work_id
    JOIN staging.work_mesh_expanded e ON e.openalex_work_id = a.openalex_work_id
    JOIN condition_mesh cm ON cm.mesh_id = e.mesh_id
),
publications AS (
    SELECT
        npi,
        condition_id,
        count(*)                                                        AS relevant_pub_count,
        count(*) FILTER (WHERE author_position IN ('first', 'last'))    AS first_last_pub_count,
        count(*) FILTER (WHERE publication_year >=
                               date_part('year', now()) - %(recency)s)  AS recent_pub_count,
        min(publication_year)                                           AS earliest_pub_year,
        max(publication_year)                                           AS latest_pub_year
    FROM publication_evidence
    GROUP BY 1, 2
),
trial_evidence AS (
    SELECT DISTINCT
        ti.npi,
        cm.condition_id,
        t.nct_id,
        t.overall_status
    FROM trial_investigator ti
    JOIN trial t ON t.nct_id = ti.nct_id
    JOIN LATERAL jsonb_array_elements_text(t.condition_mesh_ids) AS mesh(id) ON true
    JOIN condition_mesh cm ON cm.mesh_id = mesh.id
    -- Held matches are excluded everywhere features are built: they are the
    -- cases we deliberately declined to be confident about.
    WHERE ti.status = 'accepted'
),
trials AS (
    SELECT
        npi,
        condition_id,
        count(*)                                                     AS trial_count,
        count(*) FILTER (WHERE overall_status = ANY(%(active)s))      AS active_trial_count
    FROM trial_evidence
    GROUP BY 1, 2
)
SELECT
    coalesce(p.npi, tr.npi),
    coalesce(p.condition_id, tr.condition_id),
    coalesce(p.relevant_pub_count, 0),
    coalesce(p.first_last_pub_count, 0),
    coalesce(p.recent_pub_count, 0),
    p.earliest_pub_year,
    p.latest_pub_year,
    coalesce(tr.trial_count, 0),
    coalesce(tr.active_trial_count, 0),
    now()
FROM publications p
FULL OUTER JOIN trials tr ON tr.npi = p.npi AND tr.condition_id = p.condition_id
-- A doctor with only trial evidence for a condition still belongs in the
-- candidate set, which is what the FULL OUTER JOIN is for.
;
"""

REBUILD_PHYSICIAN_FEATURES = """
TRUNCATE physician_features;

INSERT INTO physician_features (
    npi, total_pub_count, total_first_last_pub_count, total_trial_count,
    grad_year, has_education, computed_at
)
SELECT
    p.npi,
    coalesce(pubs.total, 0),
    coalesce(pubs.first_last, 0),
    coalesce(trials.total, 0),
    edu.grad_year,
    -- "A non-'OTHER' med school on file". `OTHER` is real data (it correlates
    -- with international training) but it is not a school, so it does not count
    -- as having education on file.
    coalesce(edu.med_school IS NOT NULL AND edu.med_school <> 'OTHER', false),
    now()
FROM physician p
LEFT JOIN (
    SELECT npi,
           count(*) AS total,
           count(*) FILTER (WHERE author_position IN ('first', 'last')) AS first_last
    FROM authorship GROUP BY 1
) pubs ON pubs.npi = p.npi
LEFT JOIN (
    SELECT npi, count(*) AS total FROM trial_investigator
    WHERE status = 'accepted' GROUP BY 1
) trials ON trials.npi = p.npi
LEFT JOIN LATERAL (
    -- One education row per source; prefer the most confident, then the most
    -- recently retrieved. At v1 there is only CMS, but the ordering is what
    -- makes adding ORCID or a state roster a data change rather than a rewrite.
    SELECT med_school, grad_year
    FROM physician_education e
    WHERE e.npi = p.npi
    ORDER BY confidence DESC, retrieved_at DESC
    LIMIT 1
) edu ON true
WHERE p.nppes_status = 'active';
"""

SUMMARY = """
SELECT
    (SELECT count(*) FROM physician_condition_features),
    (SELECT count(DISTINCT npi) FROM physician_condition_features),
    (SELECT count(DISTINCT condition_id) FROM physician_condition_features),
    (SELECT count(*) FROM physician_features),
    (SELECT count(*) FROM physician_features WHERE total_pub_count > 0),
    (SELECT count(*) FROM physician_features WHERE has_education)
"""


def run(conn: psycopg.Connection) -> dict:
    """Rebuild both feature tables from scratch.

    Truncate-and-rebuild rather than incremental: everything here is derived from
    tables we already hold, the cost is minutes, and an incremental update that
    misses a deletion leaves a physician ranked on evidence that no longer
    exists — a silent correctness bug in the one place the product cannot afford
    one.
    """
    from ..db import analyze as analyze_tables

    with conn.cursor() as cur:
        log.info("rebuilding physician_condition_features")
        cur.execute(
            REBUILD_CONDITION_FEATURES,
            {"recency": RECENCY_YEARS, "active": list(ACTIVE_TRIAL_STATUSES)},
        )
        conn.commit()

        log.info("rebuilding physician_features")
        cur.execute(REBUILD_PHYSICIAN_FEATURES)
        conn.commit()

        cur.execute(SUMMARY)
        (pcf_rows, pcf_npis, pcf_conditions, pf_rows, pf_published, pf_educated) = cur.fetchone()

    analyze_tables(conn, "physician_condition_features", "physician_features")

    log.info(
        "features: %s condition rows across %s physicians and %s conditions; "
        "%s physician rows (%s with publications, %s with a medical school)",
        f"{pcf_rows:,}", f"{pcf_npis:,}", f"{pcf_conditions:,}",
        f"{pf_rows:,}", f"{pf_published:,}", f"{pf_educated:,}",
    )
    if pcf_rows == 0:
        log.warning(
            "no condition features were built — this needs conditions (bucket 5) "
            "AND matched publications or trials (buckets 3/4) to exist"
        )
    return {
        "condition_feature_rows": pcf_rows,
        "physicians_with_features": pcf_npis,
        "conditions_covered": pcf_conditions,
        "physician_feature_rows": pf_rows,
        "physicians_with_publications": pf_published,
        "physicians_with_education": pf_educated,
    }
