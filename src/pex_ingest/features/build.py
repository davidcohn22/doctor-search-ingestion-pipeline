"""Rebuild the precomputed search features from the concept vocabulary's three tiers.

These are the "ingredients" the ranking mixes per query. They are rebuilt as the
last step of every ingestion run, **never written at request time**, and final
scores are never stored — the product's philosophy is that the evidence is the
product and the ranking is a transparent aggregation over it, so what gets stored
are the counts a user could check, not an opaque number.

How a paper gets its tier for a concept (Conditions & Search Refactor, 2026-10-11):

  1. **Title** — every title is matched against every concept's card terms
     (condition_term) in one pass (vocabulary/corpus.py, about a minute). A
     Tier 1 term makes the paper Tier 1, a Tier 2 term Tier 2. A Tier 1 hit on
     one of the concept's own names also sets `name_hit`, for the exact-name bonus.
  2. **MeSH** — a paper tagged with one of the concept's MeSH descriptors
     (condition_mesh) lands in that row's tier (Tier 2): it catches on-topic
     papers whose titles miss the card's terms.
  3. Each paper keeps its **best** tier per concept.
  4. **Tier 3** — any paper in the concept's specialty: the paper's primary
     OpenAlex topic rolls up to a subfield listed for one of the concept's NUCC
     codes (specialty_field). Each paper has exactly one primary subfield, so a
     doctor's Tier 3 count is a sum over subfields, minus papers already counted
     in Tier 1 or 2.

Trials are tiered by the same cards: their titles, listed conditions and
interventions, plus their condition and intervention MeSH ids. Tier 3 doesn't
apply to trials.

What counts: research output only (article, review, conference paper, book
chapter, letter — backend/ranking-formula.md), from accepted author links, and
not set aside by the per-paper filter (authorship.excluded_reason).

A `physician_condition_features` row exists only when the doctor has Tier 1 or
Tier 2 evidence (papers or trials). Tier 3 adds to a score but never creates a
candidate on its own, which keeps the candidate pool tight.
"""

from __future__ import annotations

import logging
import time

import psycopg

from ..db import analyze as analyze_tables
from ..db import copy_rows

log = logging.getLogger("pex.features")

RECENCY_YEARS = 5

ACTIVE_TRIAL_STATUSES = (
    "RECRUITING",
    "ACTIVE_NOT_RECRUITING",
    "ENROLLING_BY_INVITATION",
    "NOT_YET_RECRUITING",
)

COUNTED_WORK_TYPES = ("article", "review", "conference-paper", "book-chapter", "letter")

HIT_COLUMNS_WORK = ["openalex_work_id", "condition_id", "tier", "name_hit"]
HIT_COLUMNS_TRIAL = ["nct_id", "condition_id", "tier", "name_hit"]

MESH_WORK_HITS = """
INSERT INTO staging.work_concept_hit (openalex_work_id, condition_id, tier, name_hit)
SELECT DISTINCT w.openalex_work_id, cm.condition_id, cm.tier, false
FROM work w
CROSS JOIN LATERAL jsonb_array_elements_text(w.mesh_terms) AS m(mesh_id)
JOIN condition_mesh cm ON cm.mesh_id = m.mesh_id
"""

MESH_TRIAL_HITS = """
INSERT INTO staging.trial_concept_hit (nct_id, condition_id, tier, name_hit)
SELECT DISTINCT t.nct_id, cm.condition_id, cm.tier, false
FROM trial t
CROSS JOIN LATERAL jsonb_array_elements_text(t.condition_mesh_ids || t.intervention_mesh_ids)
     AS m(mesh_id)
JOIN condition_mesh cm ON cm.mesh_id = m.mesh_id
"""

BEST_TIERS = """
CREATE TEMP TABLE work_tier AS
SELECT openalex_work_id, condition_id, min(tier) AS tier,
       bool_or(name_hit AND tier = 1) AS name_hit
FROM staging.work_concept_hit
GROUP BY 1, 2;
CREATE INDEX ON work_tier (openalex_work_id);

CREATE TEMP TABLE trial_tier AS
SELECT nct_id, condition_id, min(tier) AS tier
FROM staging.trial_concept_hit
GROUP BY 1, 2;

-- One primary subfield per paper: its first (highest-scoring) OpenAlex topic.
CREATE TEMP TABLE work_subfield AS
SELECT w.openalex_work_id, t.subfield_id
FROM work w
JOIN staging.openalex_topic t ON t.openalex_topic_id = w.topic_ids ->> 0
WHERE t.subfield_id IS NOT NULL;
CREATE INDEX ON work_subfield (openalex_work_id);

-- The papers that count as evidence at all.
CREATE TEMP TABLE counted AS
SELECT a.npi, a.openalex_work_id,
       a.author_position IN ('first', 'last') AS first_last,
       w.publication_year
FROM authorship a
JOIN physician_author pa ON pa.npi = a.npi AND pa.openalex_author_id = a.openalex_author_id
JOIN work w ON w.openalex_work_id = a.openalex_work_id
WHERE pa.status = 'accepted'
  AND a.excluded_reason IS NULL
  AND w.work_type = ANY(%(types)s);
CREATE INDEX ON counted (openalex_work_id);

CREATE TEMP TABLE concept_subfield AS
SELECT DISTINCT ct.condition_id, sf.openalex_subfield_id AS subfield_id
FROM condition_taxonomy ct
JOIN specialty_field sf ON sf.taxonomy_code = ct.taxonomy_code;
CREATE UNIQUE INDEX ON concept_subfield (condition_id, subfield_id);

CREATE TEMP TABLE doctor_subfield AS
SELECT c.npi, ws.subfield_id,
       count(*) FILTER (WHERE c.first_last) AS fl,
       count(*) FILTER (WHERE NOT c.first_last) AS mid
FROM counted c
JOIN work_subfield ws ON ws.openalex_work_id = c.openalex_work_id
GROUP BY 1, 2;
CREATE INDEX ON doctor_subfield (npi, subfield_id);
"""

EVIDENCE = """
CREATE TEMP TABLE evidence AS
SELECT c.npi, t.condition_id, c.openalex_work_id, c.first_last, c.publication_year,
       t.tier, t.name_hit,
       -- One primary subfield per paper, one row per (concept, subfield): this
       -- join can't duplicate a row, only say whether the paper is in-specialty.
       cs.condition_id IS NOT NULL AS in_specialty
FROM counted c
JOIN work_tier t ON t.openalex_work_id = c.openalex_work_id
LEFT JOIN work_subfield ws ON ws.openalex_work_id = c.openalex_work_id
LEFT JOIN concept_subfield cs
       ON cs.condition_id = t.condition_id AND cs.subfield_id = ws.subfield_id;
"""

REBUILD_CONDITION_FEATURES = """
INSERT INTO physician_condition_features (
    npi, condition_id, relevant_pub_count, first_last_pub_count, recent_pub_count,
    earliest_pub_year, latest_pub_year, trial_count, active_trial_count,
    tier1_fl_pub_count, tier1_mid_pub_count, tier1_name_fl_pub_count, tier1_name_mid_pub_count,
    tier2_fl_pub_count, tier2_mid_pub_count, tier3_fl_pub_count, tier3_mid_pub_count,
    tier1_trial_count, tier2_trial_count, computed_at
)
WITH pubs AS (
    SELECT npi, condition_id,
           count(*)                                                        AS relevant,
           count(*) FILTER (WHERE first_last)                              AS first_last,
           count(*) FILTER (WHERE publication_year >=
                                  date_part('year', now()) - %(recency)s)  AS recent,
           min(publication_year)                                           AS earliest,
           max(publication_year)                                           AS latest,
           count(*) FILTER (WHERE tier = 1 AND first_last)                 AS t1_fl,
           count(*) FILTER (WHERE tier = 1 AND NOT first_last)             AS t1_mid,
           count(*) FILTER (WHERE tier = 1 AND name_hit AND first_last)    AS t1n_fl,
           count(*) FILTER (WHERE tier = 1 AND name_hit AND NOT first_last) AS t1n_mid,
           count(*) FILTER (WHERE tier = 2 AND first_last)                 AS t2_fl,
           count(*) FILTER (WHERE tier = 2 AND NOT first_last)             AS t2_mid,
           count(*) FILTER (WHERE in_specialty AND first_last)             AS spec_fl,
           count(*) FILTER (WHERE in_specialty AND NOT first_last)         AS spec_mid
    FROM evidence
    GROUP BY 1, 2
),
trials AS (
    SELECT ti.npi, tt.condition_id,
           count(*)                                                   AS total,
           count(*) FILTER (WHERE t.overall_status = ANY(%(active)s))  AS active,
           count(*) FILTER (WHERE tt.tier = 1)                        AS t1,
           count(*) FILTER (WHERE tt.tier = 2)                        AS t2
    FROM trial_investigator ti
    JOIN trial t ON t.nct_id = ti.nct_id
    JOIN trial_tier tt ON tt.nct_id = ti.nct_id
    -- Held matches are excluded everywhere features are built: they are the
    -- cases we deliberately declined to be confident about.
    WHERE ti.status = 'accepted'
    GROUP BY 1, 2
),
rows AS (
    SELECT coalesce(p.npi, tr.npi) AS npi,
           coalesce(p.condition_id, tr.condition_id) AS condition_id,
           p.relevant, p.first_last, p.recent, p.earliest, p.latest,
           p.t1_fl, p.t1_mid, p.t1n_fl, p.t1n_mid, p.t2_fl, p.t2_mid,
           p.spec_fl, p.spec_mid,
           tr.total AS trial_total, tr.active AS trial_active,
           tr.t1 AS trial_t1, tr.t2 AS trial_t2
    FROM pubs p
    FULL OUTER JOIN trials tr ON tr.npi = p.npi AND tr.condition_id = p.condition_id
),
specialty AS (
    -- Every counted paper of the doctor in the concept's specialty subfields.
    SELECT r.npi, r.condition_id, sum(ds.fl) AS fl, sum(ds.mid) AS mid
    FROM rows r
    JOIN concept_subfield cs ON cs.condition_id = r.condition_id
    JOIN doctor_subfield ds ON ds.npi = r.npi AND ds.subfield_id = cs.subfield_id
    GROUP BY 1, 2
)
SELECT r.npi, r.condition_id,
       coalesce(r.relevant, 0), coalesce(r.first_last, 0), coalesce(r.recent, 0),
       r.earliest, r.latest,
       coalesce(r.trial_total, 0), coalesce(r.trial_active, 0),
       coalesce(r.t1_fl, 0), coalesce(r.t1_mid, 0), coalesce(r.t1n_fl, 0), coalesce(r.t1n_mid, 0),
       coalesce(r.t2_fl, 0), coalesce(r.t2_mid, 0),
       -- Tier 3 = specialty papers not already counted in Tier 1 or 2.
       greatest(coalesce(s.fl, 0) - coalesce(r.spec_fl, 0), 0),
       greatest(coalesce(s.mid, 0) - coalesce(r.spec_mid, 0), 0),
       coalesce(r.trial_t1, 0), coalesce(r.trial_t2, 0),
       now()
FROM rows r
LEFT JOIN specialty s ON s.npi = r.npi AND s.condition_id = r.condition_id
"""

REBUILD_PHYSICIAN_FEATURES = """
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
    SELECT npi, count(*) AS total, count(*) FILTER (WHERE first_last) AS first_last
    FROM counted GROUP BY 1
) pubs ON pubs.npi = p.npi
LEFT JOIN (
    SELECT npi, count(*) AS total FROM trial_investigator
    WHERE status = 'accepted' GROUP BY 1
) trials ON trials.npi = p.npi
LEFT JOIN LATERAL (
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


def term_index(conn: psycopg.Connection):
    """A TermIndex of every concept's card terms, abbreviations matched in capitals."""
    from ..vocabulary.merge import is_abbreviation
    from ..vocabulary.titles import TermIndex, normalize

    with conn.cursor() as cur:
        cur.execute("SELECT condition_id, synonym FROM condition_synonym "
                    "WHERE kind = 'abbreviation'")
        abbreviations = {(cid, normalize(s)) for cid, s in cur.fetchall()}
        cur.execute("SELECT condition_id, term, tier, is_name FROM condition_term")
        rows = cur.fetchall()
    index = TermIndex()
    added = 0
    for cid, term, tier, is_name in rows:
        abbreviation = (cid, normalize(term)) in abbreviations or is_abbreviation(term)
        added += index.add(cid, term, tier, is_name, abbreviation)
    log.info("term index: %s of %s terms matchable", f"{added:,}", f"{len(rows):,}")
    return index


def _hits(stream):
    for key, hits in stream:
        for cid, tier, name_hit in hits:
            yield (key, cid, tier, name_hit)


def match_corpus(conn: psycopg.Connection) -> dict:
    """Fill staging.work_concept_hit / trial_concept_hit from titles and MeSH."""
    from ..vocabulary.corpus import match_trials, match_works

    index = term_index(conn)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE staging.work_concept_hit, staging.trial_concept_hit")
    conn.commit()

    started = time.monotonic()
    with psycopg.connect(conn.info.dsn, password=conn.info.password) as reader:
        work_rows = copy_rows(conn, "staging.work_concept_hit", HIT_COLUMNS_WORK,
                              _hits(match_works(reader, index)))
        trial_rows = copy_rows(conn, "staging.trial_concept_hit", HIT_COLUMNS_TRIAL,
                               _hits(match_trials(reader, index)))
    log.info("title matching: %s paper hits, %s trial hits in %.0fs",
             f"{work_rows:,}", f"{trial_rows:,}", time.monotonic() - started)

    with conn.cursor() as cur:
        cur.execute(MESH_WORK_HITS)
        mesh_work = cur.rowcount
        cur.execute(MESH_TRIAL_HITS)
        mesh_trial = cur.rowcount
    conn.commit()
    analyze_tables(conn, "staging.work_concept_hit", "staging.trial_concept_hit")
    return {"title_work_hits": work_rows, "title_trial_hits": trial_rows,
            "mesh_work_hits": mesh_work, "mesh_trial_hits": mesh_trial}


def run(conn: psycopg.Connection, reuse_hits: bool = False) -> dict:
    """Rebuild both feature tables from scratch.

    Truncate-and-rebuild rather than incremental: everything here is derived from
    tables we already hold, the cost is minutes, and an incremental update that
    misses a deletion leaves a physician ranked on evidence that no longer
    exists — a silent correctness bug in the one place the product cannot afford
    one.
    """
    # reuse_hits: keep the last run's title/MeSH hits (vocabulary and papers
    # unchanged) and only recount — for re-running after a counting fix.
    stats = {} if reuse_hits else match_corpus(conn)

    with conn.cursor() as cur:
        cur.execute("SET work_mem = '1GB'")
        log.info("assigning best tiers and counting evidence")
        for statement in BEST_TIERS.split(";"):
            if statement.strip():
                params = {"types": list(COUNTED_WORK_TYPES)} if "%(" in statement else None
                cur.execute(statement, params)
        cur.execute(EVIDENCE)
        cur.execute("ANALYZE evidence")

        log.info("rebuilding physician_condition_features")
        cur.execute("TRUNCATE physician_condition_features")
        cur.execute(
            REBUILD_CONDITION_FEATURES,
            {"recency": RECENCY_YEARS, "active": list(ACTIVE_TRIAL_STATUSES)},
        )

        log.info("rebuilding physician_features")
        cur.execute("TRUNCATE physician_features")
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
            "no condition features were built — this needs the concept vocabulary "
            "AND matched publications or trials to exist"
        )
    return {
        **stats,
        "condition_feature_rows": pcf_rows,
        "physicians_with_features": pcf_npis,
        "conditions_covered": pcf_conditions,
        "physician_feature_rows": pf_rows,
        "physicians_with_publications": pf_published,
        "physicians_with_education": pf_educated,
    }
