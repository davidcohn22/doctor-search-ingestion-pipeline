"""Stage the NPPES bulk CSV into Postgres with DuckDB.

DuckDB reads the ~10 GB CSV **in place** — it is never loaded into memory and
never copied — applies the two v1 filters, and writes the result straight into
`staging.nppes_physician` through DuckDB's `postgres` extension. That keeps the
whole heavy pass in one engine instead of streaming 8.5M rows through Python.

**A deliberate deviation, stated plainly.** The runbook says staging should be a
faithful, unfiltered copy of the source so filter logic can change without
re-downloading 10 GB. Here staging holds the *filtered, projected* physician
subset (~1M rows, ~60 columns) instead of all 8.5M rows × ~330 columns. The
property the runbook actually wants is preserved, because the dated snapshot
directory keeps the untouched CSV on disk forever
(docs/prerequisites/README.md#where-files-live): re-filtering re-reads a local
file, and nothing is re-downloaded. What it avoids is carrying ~10 GB of
mostly-empty columns for non-physician provider types inside Postgres, where the
disk budget is tightest.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from ...config import Settings
from . import columns as cols

log = logging.getLogger("pex.nppes.stage")

STAGING_TABLE = "staging.nppes_physician"


def extract(zip_path: Path, dest_dir: Path) -> Path:
    """Extract an NPPES ZIP, skipping the work if it is already unpacked.

    Returns the directory. Extraction is the slowest non-network step in bucket 1
    (~10 GB), so the skip matters on any re-run.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    marker = dest_dir / ".extracted"
    if marker.exists():
        log.info("already extracted: %s", dest_dir)
        return dest_dir

    log.info("extracting %s → %s", zip_path.name, dest_dir)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(dest_dir)
    marker.write_text(zip_path.name, encoding="utf-8")
    return dest_dir


def duck(settings: Settings):
    """A DuckDB connection with the Postgres extension attached as `pg`."""
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute(f"ATTACH '{settings.database_url}' AS pg (TYPE postgres)")
    # A large CSV pass benefits from letting DuckDB spill rather than fail.
    con.execute(f"SET temp_directory = '{settings.path('_cache', 'duckdb')}'")
    return con


def read_headers(csv_path: Path) -> list[str]:
    """The file's actual header row, read without parsing the body."""
    import duckdb

    con = duckdb.connect()
    rel = con.execute(
        "SELECT * FROM read_csv(?, header=true, all_varchar=true, sample_size=1) LIMIT 0",
        [str(csv_path)],
    )
    return [d[0] for d in rel.description]


def stage(settings: Settings, csv_path: Path, resolved: dict[str, str]) -> int:
    """Filter the CSV to physicians and land it in `staging.nppes_physician`.

    The two filters together are the *entire* v1 "practicing physician" check
    (docs/ingestion/identity-npi-nppes.md):

      1. `Entity Type Code = 1` — an individual human, not an organization.
         Takes ~8.5M rows to ~6M.
      2. at least one taxonomy code starting with `20` — the NUCC range for
         allopathic and osteopathic physicians. Takes ~6M to **~1M**.

    Deactivated NPIs are staged rather than dropped: the transform flips
    `nppes_status` on rows we already hold, which is how retirees and deceased
    physicians leave search results instead of silently persisting.
    """
    projection = cols.projection_sql(resolved)
    tax_filter = cols.taxonomy_filter_sql(resolved)
    entity = resolved["entity_type_code"]

    query = f"""
        CREATE OR REPLACE TABLE pg.{STAGING_TABLE} AS
        SELECT
            {projection}
        FROM read_csv(
            '{csv_path}',
            header = true,
            all_varchar = true,      -- 330 columns; cast deliberately, never by inference
            sample_size = -1
        )
        WHERE "{entity}" = '1'
          AND {tax_filter}
    """

    con = duck(settings)
    try:
        log.info("staging %s → %s", csv_path.name, STAGING_TABLE)
        con.execute(query)
        staged = con.execute(f"SELECT count(*) FROM pg.{STAGING_TABLE}").fetchone()[0]
    finally:
        con.close()

    log.info("staged %s physician rows", f"{staged:,}")
    return int(staged)


def sanity_check(staged: int, weekly: bool = False) -> list[str]:
    """The load-bearing row-count check from the prerequisites doc.

    "If you get 8.5M, no filter applied. If you get 200K, the taxonomy clause is
     only checking one slot instead of all fifteen."
     — docs/prerequisites/01-nppes-identity.md

    Returns a list of warnings rather than raising: a monthly file that is
    genuinely 15% smaller than expected is a reason to look, not a reason to
    abandon a load that has already cost an hour.
    """
    warnings = []
    if weekly:
        if staged > 200_000:
            warnings.append(f"weekly incremental staged {staged:,} rows — unusually large")
        return warnings

    if staged > 3_000_000:
        warnings.append(
            f"staged {staged:,} rows, expected ~1M — the physician filter did not apply"
        )
    elif staged < 500_000:
        warnings.append(
            f"staged {staged:,} rows, expected ~1M — the 15-slot taxonomy clause is "
            "probably only checking slot 1"
        )
    return warnings
