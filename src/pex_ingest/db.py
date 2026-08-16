"""PostgreSQL access: connections, batched upserts, and bulk COPY.

Two rules from docs/prerequisites/README.md#conventions-every-bucket-follows are
enforced here rather than left to each bucket:

  * **Every write is an upsert** — `INSERT … ON CONFLICT … DO UPDATE`, keyed on
    the source's own identifier. That is what makes a job safe to re-run after a
    crash or a double-fire, which is the whole basis for unattended scheduling.
  * **ANALYZE after every bulk load** — Postgres does not update planner
    statistics automatically after a large insert, and the first query against a
    freshly loaded table can be pathologically slow without it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .config import Settings

log = logging.getLogger("pex.db")

__all__ = ["Jsonb", "connect", "upsert", "copy_rows", "analyze", "table_count", "chunked"]


@contextmanager
def connect(settings: Settings, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Open a connection as the ingestion role.

    The pipeline connects as `pex_ingest` (read/write on public + staging), never
    as a superuser — the .NET API's `pex_app` role is read-only on the serving
    schema, so "the API never writes" is enforced by the database rather than by
    discipline (docs/prerequisites/00-workstation.md).
    """
    with psycopg.connect(settings.database_url, autocommit=autocommit) as conn:
        yield conn


def chunked(rows: Iterable[Any], size: int) -> Iterator[list]:
    """Group an iterable into lists of `size`, without materializing the source.

    Every bulk path here streams: the NPPES CSV, the OpenAlex parquet parts and
    the ClinicalTrials.gov pager all produce far more rows than fit in memory.
    """
    batch: list = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def upsert(
    conn: psycopg.Connection,
    statement: str,
    rows: Iterable[dict],
    batch_size: int = 5_000,
    commit_each_batch: bool = True,
) -> int:
    """Run an `INSERT … ON CONFLICT` over a stream of row dicts.

    Committing per batch (the default) is deliberate for the multi-hour jobs: a
    crash at hour three keeps everything written up to the last batch, and the
    re-run skips it cheaply because every statement is an upsert. Pass
    `commit_each_batch=False` when a caller needs the whole write to be one
    transaction — the trials delta does, because its delete-then-reinsert of a
    trial's person mentions must not be observable half-done.
    """
    written = 0
    with conn.cursor() as cur:
        for batch in chunked(rows, batch_size):
            cur.executemany(statement, batch)
            written += len(batch)
            if commit_each_batch:
                conn.commit()
            if written % (batch_size * 10) == 0:
                log.info("  … %s rows written", f"{written:,}")
    if not commit_each_batch:
        conn.commit()
    return written


def copy_rows(
    conn: psycopg.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence],
    batch_size: int = 50_000,
) -> int:
    """Bulk-load with COPY — far faster than INSERT for first loads.

    COPY cannot express ON CONFLICT, so it is only used where the target is a
    staging table we truncate and rebuild (the 11.2M-row OpenAlex author pool,
    the trial person index). Serving tables always go through `upsert`.
    """
    written = 0
    target = sql.SQL("{} ({})").format(
        sql.SQL(table),  # trusted, internal constant — never user input
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
    )
    with conn.cursor() as cur, cur.copy(
        sql.SQL("COPY {} FROM STDIN").format(target)
    ) as copy:
        for batch in chunked(rows, batch_size):
            for row in batch:
                copy.write_row(row)
            written += len(batch)
            if written % (batch_size * 5) == 0:
                log.info("  … %s rows copied", f"{written:,}")
    conn.commit()
    return written


def truncate(conn: psycopg.Connection, table: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("TRUNCATE {}").format(sql.SQL(table)))
    conn.commit()


def analyze(conn: psycopg.Connection, *tables: str) -> None:
    """Refresh planner statistics after a bulk load. Never skip this."""
    with conn.cursor() as cur:
        for table in tables:
            log.info("ANALYZE %s", table)
            cur.execute(sql.SQL("ANALYZE {}").format(sql.SQL(table)))
    conn.commit()


def table_count(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.SQL(table)))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def scalar(conn: psycopg.Connection, query: str, params: Sequence | dict | None = None) -> Any:
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    return row[0] if row else None


def fetch_all(conn: psycopg.Connection, query: str, params: Sequence | dict | None = None) -> list:
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def iter_rows(
    conn: psycopg.Connection,
    query: str,
    params: Sequence | dict | None = None,
    name: str = "pex_stream",
    itersize: int = 10_000,
) -> Iterator[tuple]:
    """Stream a large result set with a server-side cursor.

    The matcher reads ~1M physician rows and the works pass reads every matched
    author; pulling either fully client-side would defeat the point of streaming
    everything else.
    """
    with conn.cursor(name=name) as cur:
        cur.itersize = itersize
        cur.execute(query, params)
        yield from cur


def apply_migrations(conn: psycopg.Connection, migrations_dir) -> list[str]:
    """Apply any `migrations/*.sql` not yet recorded as applied.

    The docs-repo DDL is the canonical schema and is deliberately not a migration
    system. These migrations are additive-only changes this repo needs on top of
    it (see migrations/README.md); each is recorded so re-running is a no-op.
    """
    from pathlib import Path

    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS staging.schema_migration (
                filename   text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute("SELECT filename FROM staging.schema_migration")
        applied = {r[0] for r in cur.fetchall()}
    conn.commit()

    ran = []
    for path in sorted(Path(migrations_dir).glob("*.sql")):
        if path.name in applied:
            continue
        log.info("applying migration %s", path.name)
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute(
                "INSERT INTO staging.schema_migration (filename) VALUES (%s)", (path.name,)
            )
        conn.commit()
        ran.append(path.name)
    return ran
