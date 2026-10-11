"""Build `staging.openalex_author_pool` — the phone book the matcher scores against.

~11.2M ever-US-affiliated authors out of OpenAlex's 119M, streamed from the
snapshot's `authors` entity (52.8 GB full, ~16 GB projected) and filtered
client-side. There is no "US only" subset to download: the snapshot is
partitioned by `updated_date`, not by geography, so the filter happens during the
streaming pass.

This is **staging, not product data** — the phone book we compare against, kept
while the matcher is tuned, then archived to Parquet and dropped (~8 GB
reclaimed) once match rates stabilize.

Two details that decide whether the matcher is usable at all:

  * **`name_key` is stored, not computed at query time.** The matcher hits it
    over a million times; a functional index would work but the stored column
    keeps the join trivial and the plan stable.
  * **The index is built *after* the load.** 11.2M rows load appreciably faster
    into a table with no indexes on it (docs/database/database-setup.md).
"""

from __future__ import annotations

import json
import logging

import psycopg

from ...config import Settings
from ...db import analyze, copy_rows, truncate
from ...names import name_key
from ...state import add_marker, get_marker
from .snapshot import AUTHOR_COLUMNS, manifest, openalex_id, read_part, save_manifest

log = logging.getLogger("pex.openalex.authors")

JOB = "openalex_author_pool"

POOL_COLUMNS = [
    "openalex_author_id", "display_name", "name_variants", "orcid",
    "affiliations", "topic_ids", "works_count", "name_key",
]

DROP_INDEX = "DROP INDEX IF EXISTS staging.openalex_author_pool_name_key"
CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS openalex_author_pool_name_key
    ON staging.openalex_author_pool (name_key)
    WHERE name_key IS NOT NULL
"""


def slim_author(record: dict, us_institutions: set[str]) -> tuple | None:
    """Keep only what matching needs; drop authors never affiliated in the US.

    The affiliation list is reduced to (institution id, latest year) pairs — the
    matcher resolves those to city/state through the institutions reference
    table, so carrying the institution's own record here would be redundant bulk
    across 11M rows.
    """
    ident = openalex_id(record.get("id"))
    if not ident:
        return None

    affiliations = []
    seen_us = False
    for affiliation in record.get("affiliations") or []:
        institution = affiliation.get("institution") or {}
        inst_id = openalex_id(institution.get("id"))
        if not inst_id:
            continue
        years = [y for y in (affiliation.get("years") or []) if y]
        affiliations.append({"inst": inst_id, "years": years})
        if inst_id in us_institutions:
            seen_us = True

    for institution in record.get("last_known_institutions") or []:
        inst_id = openalex_id((institution or {}).get("id"))
        if inst_id and inst_id in us_institutions:
            seen_us = True

    # "Ever US-affiliated" rather than "currently": physicians move, OpenAlex
    # affiliation years lag reality, and excluding someone who spent a decade at
    # a US hospital because their newest paper lists a sabbatical abroad would be
    # a pure false negative.
    if not seen_us:
        return None

    display_name = record.get("display_name") or ""
    variants = [v for v in (record.get("display_name_alternatives") or []) if v]
    orcid = (record.get("orcid") or "").rsplit("/", 1)[-1] or None
    topics = [openalex_id(t.get("id")) for t in (record.get("topics") or [])[:10]]

    return (
        ident,
        display_name,
        json.dumps(variants[:20]),
        orcid,
        json.dumps(affiliations[:20]),
        json.dumps([t for t in topics if t]),
        int(record.get("works_count") or 0),
        pool_name_key(display_name, variants),
    )


def pool_name_key(display_name: str, variants: list[str]) -> str | None:
    """The display name's key, else the first alternative spelling that has one.

    ~14% of US authors have an initial-only display name ("W. Scott Young"),
    which `name_key` rejects on purpose. Most prolific ones also list a full-name
    variant in OpenAlex; keying on it makes them retrievable, and scoring is
    unaffected because the matcher already compares every variant.
    """
    key = name_key(display_name)
    if key:
        return key
    for variant in variants:
        key = name_key(variant)
        if key:
            return key
    return None


def build(conn: psycopg.Connection, settings: Settings, http, resume: bool = True) -> dict:
    """Stream the authors snapshot into the pool table.

    Resumable at part-file granularity: each completed part is recorded in
    `staging.ingest_state`, so an interrupted multi-hour pass picks up where it
    stopped rather than restarting. That is not a nicety — a job that restarts
    from zero on a dropped connection will never finish.
    """
    from .reference import us_institution_ids

    us_institutions = us_institution_ids(conn)
    if not us_institutions:
        raise RuntimeError(
            "No US institutions in staging.openalex_institution — load the reference "
            "sets first (pex run publications --reference). Without them every author "
            "would be filtered out as non-US."
        )
    log.info("US institution filter covers %s institutions", f"{len(us_institutions):,}")

    parts = manifest(http, "authors")
    save_manifest(parts, settings.path("openalex", "authors", "manifest.json"))

    done = get_marker(conn, JOB) if resume else set()
    if done:
        log.info("resuming: %d of %d part files already loaded", len(done), len(parts))
    else:
        truncate(conn, "staging.openalex_author_pool")
        with conn.cursor() as cur:
            cur.execute(DROP_INDEX)   # loading is much faster without it
        conn.commit()

    stats = {"parts": 0, "records": 0, "kept": 0}
    for part in parts:
        if part.name in done:
            continue

        def rows():
            for record in read_part(http, part, AUTHOR_COLUMNS):
                stats["records"] += 1
                slim = slim_author(record, us_institutions)
                if slim is not None:
                    stats["kept"] += 1
                    yield slim

        copy_rows(conn, "staging.openalex_author_pool", POOL_COLUMNS, rows())
        add_marker(conn, JOB, part.name)
        stats["parts"] += 1
        if stats["parts"] % 25 == 0:
            log.info(
                "  … %d/%d parts, %s authors kept of %s read",
                stats["parts"], len(parts), f"{stats['kept']:,}", f"{stats['records']:,}",
            )

    log.info("building the name-key index (this is the one that has to be fast)")
    with conn.cursor() as cur:
        # A bigger maintenance_work_mem makes the 11.2M-row index build markedly
        # quicker; it is a session setting, so it costs nothing afterwards.
        cur.execute("SET maintenance_work_mem = '1GB'")
        cur.execute(CREATE_INDEX)
    conn.commit()
    analyze(conn, "staging.openalex_author_pool")

    log.info("author pool: %s rows kept from %s read", f"{stats['kept']:,}", f"{stats['records']:,}")
    return stats


def sanity_checks(conn: psycopg.Connection) -> list[str]:
    """The pool checks from docs/prerequisites/03-publications.md#sanity-checks."""
    warnings = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE name_key IS NOT NULL),
                   count(*) FILTER (WHERE orcid IS NOT NULL),
                   count(*) FILTER (WHERE jsonb_array_length(name_variants) > 0)
            FROM staging.openalex_author_pool
            """
        )
        total, keyed, with_orcid, with_variants = cur.fetchone()

    if not 5_000_000 <= total <= 20_000_000:
        warnings.append(f"author pool has {total:,} rows — expected ~11M US-affiliated")
    if total and keyed / total < 0.9:
        warnings.append(
            f"only {100 * keyed / total:.1f}% of pool rows have a name key — "
            "name normalization is rejecting too much"
        )
    if total and with_variants / total < 0.3:
        warnings.append(
            f"only {100 * with_variants / total:.1f}% have name variants — the "
            "display_name_alternatives column may not be projected"
        )
    # A minority carrying an ORCID is expected: ORCID coverage is genuinely
    # partial, so this is reported rather than warned about.
    log.info(
        "pool: %s rows, %s keyed, %s with ORCID, %s with name variants",
        f"{total:,}", f"{keyed:,}", f"{with_orcid:,}", f"{with_variants:,}",
    )
    return warnings


def archive(conn: psycopg.Connection, settings: Settings) -> None:
    """Archive the pool to Parquet and drop it — reclaims ~8 GB.

    Run once match rates stabilize. Kept as an explicit command rather than
    automatic cleanup, because re-running the matcher needs the pool and the
    decision that "the matcher is stable now" is a human one.
    """
    from ..nppes.stage import duck

    target = settings.path("_archive", "openalex_author_pool.parquet")
    con = duck(settings)
    try:
        con.execute(
            f"COPY (SELECT * FROM pg.staging.openalex_author_pool) "
            f"TO '{target}' (FORMAT parquet, COMPRESSION zstd)"
        )
    finally:
        con.close()
    log.info("archived the author pool to %s", target)
    log.info("drop it with: psql -c 'TRUNCATE staging.openalex_author_pool'")
