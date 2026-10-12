"""Per-paper filter: split merged OpenAlex profiles by keeping only papers that fit.

OpenAlex sometimes merges several same-name people into one author profile (a
2019 graduate credited with 886 papers going back to 1940). Rather than dropping
the whole match, each paper is judged on its own, because each paper records
where *its* author was (authorship.institution_ids / country_codes, captured by
the works pass). A paper is set aside — `authorship.excluded_reason`, never
deleted — when:

  * before_career_window — published more than CAREER_LEAD_YEARS before the
    doctor graduated from medical school (decided 2026-10-11). Without a
    graduation year, the NPI issue year stands in when the NPI was issued after
    2008 (NPIs are issued at the start of residency); otherwise only an absolute
    floor applies — nothing before EARLIEST_CAREER_YEAR, since a doctor still
    practicing who published then would be past 90 (an 1850 paper on a pediatric
    urologist with no graduation year on file).
  * non_us_affiliation — the paper lists the author only at non-US institutions.
    International medical graduates keep foreign papers from before they came to
    the US (published no later than a year after their NPI was issued).

A paper on the doctor's own ORCID works list (ORCID check verdict "confirmed")
is always kept, whatever the rules above say.

Last resort: a link that loses at least HOLD_SHARE of at least HOLD_MIN_PAPERS
papers is mostly someone else, and the few papers left can't be trusted either.
It is demoted to 'held' (signal merged_profile_unresolvable); the removal step
then deletes its papers.

Re-runnable: every run recomputes every accepted row's reason from scratch.
"""

from __future__ import annotations

import logging

import psycopg

log = logging.getLogger("pex.publications.paper_filter")

CAREER_LEAD_YEARS = 8
NPI_YEAR_PROXY_AFTER = 2008
EARLIEST_CAREER_YEAR = 1960
HOLD_SHARE = 0.90
HOLD_MIN_PAPERS = 50

CLASSIFY = """
CREATE TEMP TABLE paper_verdict AS
WITH edu AS (
    SELECT DISTINCT ON (npi) npi, med_school, grad_year
    FROM physician_education
    ORDER BY npi, confidence DESC, retrieved_at DESC
),
doctor AS (
    SELECT p.npi,
           CASE
               WHEN edu.grad_year IS NOT NULL THEN edu.grad_year - %(lead)s
               WHEN extract(year FROM p.enumeration_date) > %(npi_after)s
                   THEN extract(year FROM p.enumeration_date)::int - %(lead)s
               ELSE %(floor)s
           END AS window_start,
           extract(year FROM p.enumeration_date)::int AS npi_year,
           coalesce(edu.med_school = 'OTHER', false) AS international_grad
    FROM physician p
    LEFT JOIN edu ON edu.npi = p.npi
)
SELECT a.npi, a.openalex_work_id, a.openalex_author_id,
       CASE
           WHEN r.orcid IS NOT NULL AND (
                    (w.doi IS NOT NULL AND r.dois ? lower(w.doi))
                 OR (w.pmid IS NOT NULL AND r.pmids ? w.pmid))
               THEN NULL
           WHEN d.window_start IS NOT NULL AND w.publication_year < d.window_start
               THEN 'before_career_window'
           WHEN jsonb_array_length(a.country_codes) > 0 AND NOT a.country_codes ? 'US'
                AND NOT (d.international_grad AND w.publication_year <= d.npi_year + 1)
               THEN 'non_us_affiliation'
       END AS reason
FROM authorship a
JOIN physician_author pa ON pa.npi = a.npi AND pa.openalex_author_id = a.openalex_author_id
JOIN work w ON w.openalex_work_id = a.openalex_work_id
JOIN doctor d ON d.npi = a.npi
LEFT JOIN staging.orcid_record r
       ON r.orcid = pa.orcid AND r.http_status = 200
      AND pa.signals ->> 'orcid_check' = 'confirmed'
WHERE pa.status = 'accepted'
"""

APPLY = """
UPDATE authorship a SET excluded_reason = v.reason
FROM paper_verdict v
WHERE a.npi = v.npi AND a.openalex_work_id = v.openalex_work_id
  AND a.excluded_reason IS DISTINCT FROM v.reason
"""

HOLD_UNRESOLVABLE = """
WITH link AS (
    SELECT npi, openalex_author_id, count(*) AS papers,
           count(*) FILTER (WHERE reason IS NOT NULL) AS excluded
    FROM paper_verdict
    GROUP BY 1, 2
)
UPDATE physician_author pa
SET status = 'held',
    signals = coalesce(pa.signals, '{}'::jsonb)
              || jsonb_build_object('merged_profile_unresolvable', true,
                                    'papers_excluded', link.excluded, 'papers', link.papers)
FROM link
WHERE pa.npi = link.npi AND pa.openalex_author_id = link.openalex_author_id
  AND pa.status = 'accepted'
  AND link.papers >= %(min_papers)s
  AND link.excluded >= %(share)s * link.papers
"""

SUMMARY = """
SELECT count(*) FILTER (WHERE reason IS NULL),
       count(*) FILTER (WHERE reason = 'before_career_window'),
       count(*) FILTER (WHERE reason = 'non_us_affiliation'),
       count(DISTINCT npi) FILTER (WHERE reason IS NOT NULL)
FROM paper_verdict
"""


def run(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute("SET work_mem = '512MB'")
        cur.execute(CLASSIFY, {"lead": CAREER_LEAD_YEARS, "npi_after": NPI_YEAR_PROXY_AFTER,
                          "floor": EARLIEST_CAREER_YEAR})
        cur.execute("CREATE INDEX ON paper_verdict (npi, openalex_work_id)")
        cur.execute("ANALYZE paper_verdict")
        cur.execute(SUMMARY)
        kept, window, foreign, doctors = cur.fetchone()
        cur.execute(APPLY)
        changed = cur.rowcount
        cur.execute(HOLD_UNRESOLVABLE, {"min_papers": HOLD_MIN_PAPERS, "share": HOLD_SHARE})
        held = cur.rowcount
        cur.execute("DROP TABLE paper_verdict")
    conn.commit()
    stats = {"papers_kept": kept, "excluded_before_career_window": window,
             "excluded_non_us_affiliation": foreign, "doctors_with_exclusions": doctors,
             "rows_changed": changed, "links_held_unresolvable": held}
    log.info("paper filter: %s", stats)
    return stats
