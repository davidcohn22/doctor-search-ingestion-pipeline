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


# A few records spell the state out ("PUERTO RICO"); store and judge those by code.
US_STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI",
    "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND",
    "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN",
    "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY", "PUERTO RICO": "PR",
    "GUAM": "GU", "VIRGIN ISLANDS": "VI", "U.S. VIRGIN ISLANDS": "VI",
    "AMERICAN SAMOA": "AS", "NORTHERN MARIANA ISLANDS": "MP",
}


def state_sql(col: str) -> str:
    """Upper-cased, trimmed state with spelled-out US names mapped to their code."""
    raw = f"upper(btrim(coalesce({col}, '')))"
    cases = " ".join(f"WHEN '{name}' THEN '{code}'" for name, code in US_STATE_NAMES.items())
    return f"(CASE {raw} {cases} ELSE nullif({raw}, '') END)"


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
    {state_sql('s.practice_state')},
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


def default_primary_sql(slots: int = TAXONOMY_SLOTS) -> str:
    """Give every physician with no flagged primary taxonomy exactly one.

    NPPES lets a provider leave the primary switch blank in every slot; the first
    live load had 11,849 such physicians, who would be invisible to any
    primary-specialty filter. The conventional reading is the first slot, and a
    physician code beats a non-physician one (e.g. "Student…") when both appear.
    """
    rows = ",\n        ".join(
        f"({n}, s.taxonomy_code_{n}, s.license_state_{n})" for n in range(1, slots + 1)
    )
    return f"""
WITH no_primary AS (
    SELECT p.npi FROM physician p
    WHERE NOT EXISTS (SELECT 1 FROM physician_taxonomy t WHERE t.npi = p.npi AND t.is_primary)
),
first_slot AS (
    SELECT DISTINCT ON (s.npi)
        s.npi, t.code, coalesce(nullif(btrim(t.license_state), ''), '') AS license_state
    FROM staging.nppes_physician s
    JOIN no_primary USING (npi)
    CROSS JOIN LATERAL (VALUES
        {rows}
    ) AS t(slot, code, license_state)
    JOIN taxonomy tx ON tx.code = t.code
    ORDER BY s.npi, tx.is_physician DESC, t.slot
)
UPDATE physician_taxonomy pt SET is_primary = true
FROM first_slot f
WHERE pt.npi = f.npi AND pt.taxonomy_code = f.code AND pt.license_state = f.license_state
"""


# ---------------------------------------------------------------------------
# Exclusions beyond the stage filter (decided 2026-10-10, first live run).
#
# The stage filter keeps anyone with a `20…` code in ANY of 15 slots, which lets
# in ~28K nurse practitioners, PAs and CRNAs who list e.g. Family Medicine as a
# secondary code. A record is dropped when it carries no MD/DO-type credential
# AND either its credential is a known non-physician one or its primary code is
# a non-physician code. MDs/DOs with an unusual primary code are kept.
#
# Practice addresses outside the 50 states, DC and the inhabited territories
# (Canada, APO/FPO military mail, abroad) are dropped: a US search can't place
# them and the geocoder can't either.
# ---------------------------------------------------------------------------

PHYSICIAN_CREDENTIALS = ("MD", "DO", "MBBS", "MBCHB", "MBBCH", "MB", "MDCM")
NON_PHYSICIAN_CREDENTIALS = (
    "NP", "FNP", "FNPC", "FNPBC", "ANP", "AGNP", "AGPCNP", "AGACNP", "ACNP", "PNP",
    "WHNP", "NNP", "PMHNP", "APRN", "APN", "CNP", "ARNP", "CRNP", "PA", "PAC",
    "CRNA", "RN", "BSN", "MSN", "DNP", "CNM", "LPN", "PHARMD", "RPH", "DDS", "DMD",
    "DPM", "OD", "DC", "PSYD", "LCSW", "PT", "DPT", "OT", "SLP", "AUD",
    "CPNP", "CFNP", "NURSE", "PTA", "ATC",
    # Seen in the spine after the first load: researchers, other doctorates,
    # therapists and techs that list a physician taxonomy.
    "PHD", "DVM", "VMD", "ND", "NMD", "LMT", "MS", "MA", "OTR", "OTRL", "COTA",
    "COTAL", "CSFA", "RDMS", "LPTA", "MPT", "RPT", "LAC", "DACM", "RD", "MSW",
    "LPC", "LMFT", "CNA", "EMT", "STUDENT",
)
# NUCC files medical physicists under the physician "Radiology" classification;
# without an MD/DO they are not physicians.
NON_PHYSICIAN_TAXONOMIES = ("2085R0205X",)
US_STATES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "PR", "GU", "VI", "AS", "MP",
)

# Credentials are free text ("M.D.", "PA-C", "MD, PhD"); compare whole tokens so
# "DMD" never reads as "MD". Periods are dropped first so "P.A." and "PH.D." read
# as PA and PHD; "M. D." still tokenizes to "M D", hence the extra cases. The MD
# check also reads the undotted form, so "MD.PHD" (→ "MDPHD") stays a physician.
_CRED_TOKENS = (
    "(' ' || regexp_replace(regexp_replace(upper(coalesce(s.credential, '')), '[.]', '', 'g'),"
    " '[^A-Z]+', ' ', 'g') || ' ')"
)
_CRED_TOKENS_RAW = (
    "(' ' || regexp_replace(upper(coalesce(s.credential, '')), '[^A-Z]+', ' ', 'g') || ' ')"
)


def _token_regex(tokens) -> str:
    return "' (" + "|".join(tokens) + ") '"


def exclusion_sql(slots: int = TAXONOMY_SLOTS) -> str:
    """One row per staged NPI to drop, with the reason."""
    primary_nonphys = " OR ".join(
        f"(upper(btrim(coalesce(s.taxonomy_primary_{n}, ''))) = 'Y' "
        f"AND (coalesce(s.taxonomy_code_{n}, '') NOT LIKE '20%' "
        f"OR s.taxonomy_code_{n} IN ({', '.join(repr(c) for c in NON_PHYSICIAN_TAXONOMIES)})))"
        for n in range(1, slots + 1)
    )
    has_md = (
        f"({_CRED_TOKENS} ~ {_token_regex(PHYSICIAN_CREDENTIALS)} "
        f"OR {_CRED_TOKENS_RAW} ~ {_token_regex(PHYSICIAN_CREDENTIALS)} "
        f"OR {_CRED_TOKENS} ~ ' M D ' OR {_CRED_TOKENS} ~ ' D O ')"
    )
    us = ", ".join(f"'{st}'" for st in US_STATES)
    state = f"coalesce({state_sql('s.practice_state')}, '')"
    return f"""
SELECT s.npi,
       CASE WHEN {state} NOT IN ({us})
            THEN 'outside_us' ELSE 'non_physician' END AS reason
FROM staging.nppes_physician s
WHERE {state} NOT IN ({us})
   OR (NOT {has_md}
       AND ({_CRED_TOKENS} ~ {_token_regex(NON_PHYSICIAN_CREDENTIALS)} OR {primary_nonphys}))
"""


def apply_exclusions(conn: psycopg.Connection, run_summary=None) -> dict[str, int]:
    """Drop excluded NPIs from staging and from anything already loaded."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS pg_temp.nppes_excluded")
        cur.execute(f"CREATE TEMP TABLE nppes_excluded AS {exclusion_sql()}")
        cur.execute("SELECT reason, count(*) FROM nppes_excluded GROUP BY reason")
        counts = dict(cur.fetchall())
        cur.execute(
            "DELETE FROM staging.nppes_physician s USING nppes_excluded e WHERE s.npi = e.npi"
        )
        # Cascades to physician_taxonomy and every other NPI-keyed table.
        cur.execute("DELETE FROM physician p USING nppes_excluded e WHERE p.npi = e.npi")
        counts["already_loaded_removed"] = cur.rowcount
    conn.commit()
    for reason in ("non_physician", "outside_us"):
        log.info("excluded %s staged rows: %s", f"{counts.get(reason, 0):,}", reason)
        if run_summary is not None and counts.get(reason):
            run_summary.reject(reason, counts[reason])
    return counts


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

    excluded = apply_exclusions(conn, run_summary)
    stats["excluded_non_physician"] = excluded.get("non_physician", 0)
    stats["excluded_outside_us"] = excluded.get("outside_us", 0)

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
        cur.execute(default_primary_sql())
        stats["primary_defaulted_to_first_slot"] = cur.rowcount
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
