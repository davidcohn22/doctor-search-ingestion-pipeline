"""Transform staged NPPES rows into `physician` and `physician_taxonomy`.

Runs entirely in SQL against `staging.nppes_physician`, so a parsing fix is a
seconds-long re-run rather than another pass over the 10 GB CSV.

Everything here is an upsert keyed on the source's own identifier (the NPI), so
the job is idempotent — safe to re-run after a crash or a double-fire, which is
what makes unattended scheduling safe rather than nerve-wracking.
"""

from __future__ import annotations

import logging

import psycopg

from ...db import analyze
from .columns import TAXONOMY_SLOTS

log = logging.getLogger("pex.nppes.transform")

# NPPES dates arrive as MM/DD/YYYY. A regex guard before `to_date` is what keeps
# one malformed value from aborting a million-row statement — Postgres has no
# TRY_CAST, and a hard failure here would be indistinguishable from a bad load.
_DATE = "CASE WHEN {col} ~ '^\\d{{2}}/\\d{{2}}/\\d{{4}}$' THEN to_date({col}, 'MM/DD/YYYY') END"


def _date(col: str) -> str:
    return _DATE.format(col=col)


UPSERT_PHYSICIAN = f"""
INSERT INTO physician (
    npi, first_name, middle_name, last_name, name_suffix, credential, gender,
    sole_proprietor, nppes_status, enumeration_date, nppes_last_updated,
    other_names, practice_address_line1, practice_address_line2, practice_city,
    practice_state, practice_zip, mailing_address, raw, retrieved_at
)
SELECT
    s.npi,
    nullif(btrim(s.first_name), ''),
    nullif(btrim(s.middle_name), ''),
    nullif(btrim(s.last_name), ''),
    nullif(btrim(s.name_suffix), ''),
    nullif(btrim(s.credential), ''),
    nullif(btrim(s.gender), ''),
    CASE upper(btrim(coalesce(s.sole_proprietor, ''))) WHEN 'Y' THEN true WHEN 'N' THEN false END,
    -- Deactivated unless a reactivation date is on or after the deactivation.
    -- This is how a retiree leaves search results: the row and its provenance
    -- survive, the status flips.
    CASE
        WHEN nullif(btrim(s.deactivation_date), '') IS NULL THEN 'active'
        WHEN {_date('s.reactivation_date')} IS NOT NULL
             AND {_date('s.reactivation_date')} >= {_date('s.deactivation_date')} THEN 'active'
        ELSE 'deactivated'
    END,
    {_date('s.enumeration_date')},
    {_date('s.last_update_date')},
    -- Alternate/prior names (maiden names, variants) as a JSON array. A key
    -- input to the publication and trial matchers, which look candidates up
    -- under every name a physician has ever used.
    CASE
        WHEN nullif(btrim(coalesce(s.other_last_name, '')), '') IS NULL THEN '[]'::jsonb
        ELSE jsonb_build_array(jsonb_strip_nulls(jsonb_build_object(
            'first_name', nullif(btrim(coalesce(s.other_first_name, '')), ''),
            'middle_name', nullif(btrim(coalesce(s.other_middle_name, '')), ''),
            'last_name',  nullif(btrim(s.other_last_name), ''),
            'credential', nullif(btrim(coalesce(s.other_credential, '')), ''),
            'type_code',  nullif(btrim(coalesce(s.other_last_name_type, '')), '')
        )))
    END,
    nullif(btrim(coalesce(s.practice_line1, '')), ''),
    nullif(btrim(coalesce(s.practice_line2, '')), ''),
    nullif(btrim(coalesce(s.practice_city, '')), ''),
    upper(nullif(btrim(coalesce(s.practice_state, '')), '')),
    nullif(btrim(coalesce(s.practice_zip, '')), ''),
    jsonb_strip_nulls(jsonb_build_object(
        'line1', nullif(btrim(coalesce(s.mailing_line1, '')), ''),
        'line2', nullif(btrim(coalesce(s.mailing_line2, '')), ''),
        'city',  nullif(btrim(coalesce(s.mailing_city, '')), ''),
        'state', nullif(btrim(coalesce(s.mailing_state, '')), ''),
        'zip',   nullif(btrim(coalesce(s.mailing_zip, '')), '')
    )),
    -- `raw` keeps the staged row minus its empty columns, so a later parsing
    -- change re-reads this instead of the source file.
    (SELECT jsonb_object_agg(key, value)
       FROM jsonb_each_text(to_jsonb(s)) AS e(key, value)
      WHERE value IS NOT NULL AND btrim(value) <> ''),
    now()
FROM staging.nppes_physician s
WHERE s.npi ~ '^\\d{{10}}$'
  AND nullif(btrim(s.last_name), '') IS NOT NULL
  AND nullif(btrim(s.first_name), '') IS NOT NULL
ON CONFLICT (npi) DO UPDATE SET
    first_name             = EXCLUDED.first_name,
    middle_name            = EXCLUDED.middle_name,
    last_name              = EXCLUDED.last_name,
    name_suffix            = EXCLUDED.name_suffix,
    credential             = EXCLUDED.credential,
    gender                 = EXCLUDED.gender,
    sole_proprietor        = EXCLUDED.sole_proprietor,
    nppes_status           = EXCLUDED.nppes_status,
    enumeration_date       = EXCLUDED.enumeration_date,
    nppes_last_updated     = EXCLUDED.nppes_last_updated,
    other_names            = EXCLUDED.other_names,
    practice_address_line1 = EXCLUDED.practice_address_line1,
    practice_address_line2 = EXCLUDED.practice_address_line2,
    practice_city          = EXCLUDED.practice_city,
    practice_state         = EXCLUDED.practice_state,
    practice_zip           = EXCLUDED.practice_zip,
    mailing_address        = EXCLUDED.mailing_address,
    raw                    = EXCLUDED.raw,
    retrieved_at           = EXCLUDED.retrieved_at
"""


def _taxonomy_values_clause(slots: int = TAXONOMY_SLOTS) -> str:
    """The 15-slot unpivot, generated rather than typed.

    Hand-writing fifteen near-identical rows is exactly how a pipeline ends up
    reading only slot 1 — which shows up as `physician_taxonomy` having roughly
    one row per doctor instead of the expected 1.5–2.
    """
    rows = ",\n        ".join(
        f"(s.taxonomy_code_{n}, s.license_state_{n}, s.license_number_{n}, s.taxonomy_primary_{n})"
        for n in range(1, slots + 1)
    )
    return f"(VALUES\n        {rows}\n    ) AS t(code, license_state, license_number, primary_switch)"


def upsert_taxonomies_sql(slots: int = TAXONOMY_SLOTS) -> str:
    return f"""
INSERT INTO physician_taxonomy (npi, taxonomy_code, license_state, license_number,
                                is_primary, retrieved_at)
SELECT DISTINCT ON (s.npi, t.code, coalesce(nullif(btrim(t.license_state), ''), ''))
    s.npi,
    t.code,
    coalesce(nullif(btrim(t.license_state), ''), ''),
    nullif(btrim(t.license_number), ''),
    upper(coalesce(btrim(t.primary_switch), '')) = 'Y',
    now()
FROM staging.nppes_physician s
CROSS JOIN LATERAL {_taxonomy_values_clause(slots)}
-- The FK into `taxonomy` means an unknown code would abort the whole statement.
-- Joining instead of trusting drops codes NUCC has retired since our reference
-- load, and the count of dropped rows is reported as a reject.
JOIN taxonomy tx ON tx.code = t.code
JOIN physician p ON p.npi = s.npi
WHERE nullif(btrim(t.code), '') IS NOT NULL
ON CONFLICT (npi, taxonomy_code, license_state) DO UPDATE SET
    license_number = EXCLUDED.license_number,
    is_primary     = EXCLUDED.is_primary,
    retrieved_at   = EXCLUDED.retrieved_at
"""


COUNT_UNKNOWN_TAXONOMIES = f"""
SELECT count(*)
FROM staging.nppes_physician s
CROSS JOIN LATERAL {_taxonomy_values_clause()}
LEFT JOIN taxonomy tx ON tx.code = t.code
WHERE nullif(btrim(t.code), '') IS NOT NULL
  AND tx.code IS NULL
"""


def run(conn: psycopg.Connection, run_summary=None) -> dict:
    """Apply both upserts and return counts for the run summary."""
    stats: dict[str, int] = {}

    with conn.cursor() as cur:
        log.info("upserting physician rows")
        cur.execute(UPSERT_PHYSICIAN)
        stats["physician_upserted"] = cur.rowcount
        conn.commit()

        cur.execute(COUNT_UNKNOWN_TAXONOMIES)
        unknown = cur.fetchone()[0]
        if unknown:
            log.warning(
                "%s taxonomy slot values are not in the NUCC reference table — "
                "refresh it (twice-yearly release) if this is large",
                f"{unknown:,}",
            )
            if run_summary is not None:
                run_summary.reject("taxonomy_code_not_in_nucc", unknown)

        log.info("upserting physician_taxonomy rows")
        cur.execute(upsert_taxonomies_sql())
        stats["taxonomy_upserted"] = cur.rowcount
        conn.commit()

        cur.execute("SELECT count(*) FILTER (WHERE nppes_status = 'active'), count(*) FROM physician")
        active, total = cur.fetchone()
        stats["physicians_active"] = active
        stats["physicians_total"] = total

    analyze(conn, "physician", "physician_taxonomy")
    log.info(
        "physician: %s rows (%s active) | physician_taxonomy: %s rows",
        f"{total:,}", f"{active:,}", f"{stats['taxonomy_upserted']:,}",
    )
    return stats


def post_load_checks(conn: psycopg.Connection) -> list[str]:
    """The sanity checks from docs/prerequisites/01-nppes-identity.md.

    Returned as warnings rather than raised: a silent mis-parse is the thing to
    catch, and these are the numbers that catch it.
    """
    warnings = []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM physician WHERE nppes_status = 'active'")
        active = cur.fetchone()[0]
        if not 500_000 <= active <= 3_000_000:
            warnings.append(f"{active:,} active physicians — expected roughly 1M")

        cur.execute(
            "SELECT round(100.0 * count(*) FILTER (WHERE practice_state IS NOT NULL) "
            "/ greatest(count(*), 1), 1) FROM physician"
        )
        pct_state = float(cur.fetchone()[0] or 0)
        if pct_state < 95:
            warnings.append(
                f"only {pct_state}% of physicians have a practice_state — "
                "the address column mapping is probably off by one"
            )

        cur.execute("SELECT count(*) FROM physician_taxonomy")
        tax_rows = cur.fetchone()[0]
        if tax_rows <= active:
            warnings.append(
                f"physician_taxonomy has {tax_rows:,} rows for {active:,} physicians — "
                "expected more than one per doctor; only slot 1 is likely being read"
            )
    return warnings
