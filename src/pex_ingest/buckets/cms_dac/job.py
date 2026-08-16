"""Bucket 2 — medical school and graduation year, from the CMS Doctors &
Clinicians National Downloadable File.

    pex run cms

The smallest and easiest of the five buckets: one CSV, no credentials, minutes of
runtime. **Bucket 1 must be loaded first** — this file covers every
Medicare-enrolled clinician type (social workers, chiropractors, physical
therapists), and only rows whose NPI already exists in `physician` are kept. Run
it against an empty spine and it inserts nothing, silently.

Three things this bucket has to get right, all of which are easy to get wrong:

  * **The file has one row per clinician × practice location.** A physician with
    four offices appears four times. Undeduplicated, that is four
    `physician_education` rows per doctor or a primary-key conflict.
  * **`Med_sch = 'OTHER'` is data, not a null.** It correlates strongly with
    international medical training, so treating it as missing throws away a real
    signal. It lands unchanged; display logic decides how to present it.
  * **`Grd_yr` needs a sanity guard.** "A graduation year of `19` or `2087` in a
    physician profile is the kind of visible-and-embarrassing error this bucket
    is otherwise too easy to ship."
"""

from __future__ import annotations

import logging
from datetime import date

from ...config import Settings
from ...db import analyze, connect
from ...http import client_for
from ...runs import RunSummary
from ...state import mark_finished

log = logging.getLogger("pex.cms")

# The direct file URL contains a content hash and changes every time CMS
# republishes, so it cannot be hardcoded. This metastore endpoint is the stable
# entry point.
METASTORE = (
    "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/mj5m-pzi6"
)

MIN_GRAD_YEAR = 1930  # matches the CHECK constraint on physician_education.grad_year

STAGING_TABLE = "staging.cms_dac_dedup"

UPSERT = """
INSERT INTO physician_education (npi, med_school, grad_year, source, source_record_id,
                                 confidence, retrieved_at, raw)
SELECT
    s.npi,
    s.med_school,
    s.grad_year,
    'cms',
    s.npi,
    -- CMS keys this by NPI directly, so there is no matching and no uncertainty.
    1.0,
    now(),
    jsonb_build_object('med_school', s.med_school, 'grd_yr', s.grad_year_raw)
FROM staging.cms_dac_dedup s
JOIN physician p USING (npi)          -- physicians only; drops every other clinician type
ON CONFLICT (npi, source) DO UPDATE SET
    med_school   = EXCLUDED.med_school,
    grad_year    = EXCLUDED.grad_year,
    retrieved_at = EXCLUDED.retrieved_at,
    raw          = EXCLUDED.raw
"""

COVERAGE = """
SELECT
    (SELECT count(*) FROM physician)                                   AS spine,
    count(*)                                                           AS with_education,
    round(100.0 * count(*) / greatest((SELECT count(*) FROM physician), 1), 1) AS pct_covered,
    round(100.0 * count(*) FILTER (WHERE med_school = 'OTHER')
          / greatest(count(*), 1), 1)                                  AS pct_other
FROM physician_education
WHERE source = 'cms'
"""


def resolve_release(http) -> tuple[str, str]:
    """(download URL, release date) from the metastore.

    `modified` is the authoritative release date, which makes "have I already got
    this release?" a string comparison instead of a re-download.
    """
    meta = http.get_json(METASTORE, use_cache=False)
    url = meta["distribution"][0]["downloadURL"]
    modified = (meta.get("modified") or date.today().isoformat())[:10]
    log.info("CMS DAC release %s → %s", modified, url)
    return url, modified


def stage(settings: Settings, csv_path) -> tuple[int, int]:
    """Dedupe to one row per NPI with DuckDB, landing in `staging.cms_dac_dedup`.

    The dedupe is clean because `Med_sch` and `Grd_yr` are identical across a
    clinician's rows — they come from that doctor's single PECOS enrollment
    record, not from the practice location — so DISTINCT on the three columns
    collapses it correctly.

    The graduation-year guard runs here too: `TRY_CAST` turns junk into NULL
    rather than aborting, and out-of-range years are counted and dropped.
    """
    from ..nppes.stage import duck

    con = duck(settings)
    try:
        raw_rows, distinct_npis = con.execute(
            f"""
            SELECT count(*), count(DISTINCT NPI)
            FROM read_csv('{csv_path}', header=true, all_varchar=true, sample_size=-1)
            """
        ).fetchone()
        log.info(
            "CMS file: %s rows, %s distinct NPIs (%.2f rows per NPI)",
            f"{raw_rows:,}", f"{distinct_npis:,}", raw_rows / max(distinct_npis, 1),
        )

        con.execute(
            f"""
            CREATE OR REPLACE TABLE pg.{STAGING_TABLE} AS
            SELECT DISTINCT
                NPI                       AS npi,
                btrim(Med_sch)            AS med_school,
                CASE
                    WHEN TRY_CAST(Grd_yr AS SMALLINT) BETWEEN {MIN_GRAD_YEAR}
                         AND {date.today().year}
                    THEN TRY_CAST(Grd_yr AS SMALLINT)
                END                       AS grad_year,
                Grd_yr                    AS grad_year_raw
            FROM read_csv('{csv_path}', header=true, all_varchar=true, sample_size=-1)
            WHERE Med_sch IS NOT NULL AND btrim(Med_sch) <> ''
              AND regexp_matches(NPI, '^[0-9]{{10}}$')
            """
        )
        staged = con.execute(f"SELECT count(*) FROM pg.{STAGING_TABLE}").fetchone()[0]
        rejected = con.execute(
            f"SELECT count(*) FROM pg.{STAGING_TABLE} WHERE grad_year IS NULL"
        ).fetchone()[0]
    finally:
        con.close()

    log.info("staged %s deduped rows (%s with an unusable Grd_yr)",
             f"{staged:,}", f"{rejected:,}")
    if raw_rows and distinct_npis and raw_rows / distinct_npis < 1.05:
        log.warning(
            "rows-per-NPI is ~1.0 — either the file changed shape or this is a "
            "pre-filtered extract; the multi-location duplication is expected"
        )
    return staged, rejected


def ingest(settings: Settings) -> None:
    http = client_for(settings, "cms")
    url, release = resolve_release(http)

    with connect(settings) as conn, RunSummary("cms_dac", conn) as run:
        run.note("release", release)
        run.report_path = settings.report_dir / f"cms_dac-{release}.json"

        path = http.download(
            url, settings.snapshot_dir("cms-dac", release) / "DAC_NationalDownloadableFile.csv"
        )

        staged, bad_years = stage(settings, path)
        run.add("staged", staged)
        if bad_years:
            run.reject("unusable_grad_year", bad_years)

        with conn.cursor() as cur:
            cur.execute(UPSERT)
            run.add("education_upserted", cur.rowcount)
        conn.commit()

        analyze(conn, "physician_education")

        with conn.cursor() as cur:
            cur.execute(COVERAGE)
            spine, covered, pct, pct_other = cur.fetchone()
        # pct_other is the number that decides whether the v2 enrichment chain
        # (ORCID → state rosters → scraped bios) is worth prioritizing. Capture
        # it from the first load so there is a baseline rather than a guess.
        log.info(
            "coverage: %s of %s spine physicians have CMS education (%s%%); %s%% are 'OTHER'",
            f"{covered:,}", f"{spine:,}", pct, pct_other,
        )
        run.note("coverage", {"spine": spine, "with_education": covered,
                              "pct_covered": float(pct or 0), "pct_other": float(pct_other or 0)})

        if spine == 0:
            log.error(
                "the physician spine is empty — this bucket just inserted nothing. "
                "Run bucket 1 first."
            )
        mark_finished(conn, "cms_dac")
