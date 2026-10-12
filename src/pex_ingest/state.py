"""Resumable-walk bookkeeping, backed by `staging.ingest_state`.

Every paginated walk (OpenAlex cursors, ClinicalTrials.gov page tokens, the
snapshot part-file list) saves its position after each batch, so a multi-hour
job continues instead of restarting. "A multi-hour job that restarts from zero
on a dropped connection will never finish."
  — docs/prerequisites/03-publications.md#resumability-is-not-optional-here

One row per job:
    job         — 'nppes_seed', 'openalex_author_pool', 'ctgov_delta', …
    cursor      — the opaque token; NULL means the walk finished
    last_run_at — used by delta queries as the "changed since" bound
    notes       — free-form run metadata (counts, errors, timings)

`cursor` is deliberately opaque: OpenAlex cursors, ctgov page tokens and our own
"last completed part file" marker all live in the same column, because the
resume contract is identical for all three.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger("pex.state")

# Sentinel written to `cursor` when a walk completed. NULL also means finished,
# but only DONE distinguishes "ran to completion" from "never started".
DONE = "__done__"


def load_cursor(conn: psycopg.Connection, job: str) -> str | None:
    """Where this job left off, or None if it never ran / already finished."""
    with conn.cursor() as cur:
        cur.execute('SELECT "cursor" FROM staging.ingest_state WHERE job = %s', (job,))
        row = cur.fetchone()
    if not row or row[0] is None or row[0] == DONE:
        return None
    return row[0]


def save_cursor(conn: psycopg.Connection, job: str, cursor: str | None) -> None:
    """Persist a walk position. Called after *every* batch, not every N batches.

    The write is its own committed transaction so the bookmark survives a crash
    in the very next batch — which is the failure this table exists for.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO staging.ingest_state (job, "cursor", updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (job) DO UPDATE SET
                "cursor" = EXCLUDED."cursor",
                updated_at = now()
            """,
            (job, cursor),
        )
    conn.commit()


def mark_started(conn: psycopg.Connection, job: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO staging.ingest_state (job, updated_at)
            VALUES (%s, now())
            ON CONFLICT (job) DO UPDATE SET updated_at = now()
            """,
            (job,),
        )
    conn.commit()


def mark_finished(conn: psycopg.Connection, job: str) -> None:
    """Clear the cursor and stamp `last_run_at`.

    `last_run_at` is the bound the next delta query uses ("everything changed
    since"), so it is only advanced on a *completed* walk — stamping it after a
    partial run would silently skip whatever the interrupted run never reached.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO staging.ingest_state (job, "cursor", last_run_at, updated_at)
            VALUES (%s, %s, now(), now())
            ON CONFLICT (job) DO UPDATE SET
                "cursor" = EXCLUDED."cursor",
                last_run_at = now(),
                updated_at = now()
            """,
            (job, DONE),
        )
    conn.commit()


def last_run_at(conn: psycopg.Connection, job: str) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("SELECT last_run_at FROM staging.ingest_state WHERE job = %s", (job,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def delta_since(conn: psycopg.Connection, job: str, default_days: int = 14) -> str:
    """The `changed since` date for a delta query, as YYYY-MM-DD.

    A job that has never completed has no bound to work from, so it falls back to
    a fixed lookback window. Overlap is harmless — every write is an upsert — and
    is much cheaper than the alternative failure, which is silently missing the
    window between the last completed run and now.
    """
    from datetime import timedelta

    stamp = last_run_at(conn, job)
    if stamp is None:
        stamp = datetime.now(timezone.utc) - timedelta(days=default_days)
        log.info("%s has no completed run; using a %d-day lookback", job, default_days)
    return stamp.date().isoformat()


def record_run(conn: psycopg.Connection, job: str, summary: dict[str, Any]) -> None:
    """Store a RunSummary's counters in `notes`, keeping the last few runs.

    Kept as a short history rather than one row so "did this week's load look
    like last week's?" is answerable — the single most useful question about an
    unattended job, and the one a bare success/fail flag cannot answer.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT notes FROM staging.ingest_state WHERE job = %s", (job,))
        row = cur.fetchone()
        history = []
        if row and row[0]:
            existing = row[0]
            history = existing.get("history", []) if isinstance(existing, dict) else []
        history = ([summary] + list(history))[:10]

        cur.execute(
            """
            INSERT INTO staging.ingest_state (job, notes, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (job) DO UPDATE SET
                notes = EXCLUDED.notes,
                updated_at = now()
            """,
            (job, Jsonb({"latest": summary, "history": history})),
        )
    conn.commit()


def get_marker(conn: psycopg.Connection, job: str) -> set[str]:
    """Read a set-shaped bookmark — used for "which part files are done".

    The snapshot download is not a linear cursor: parts complete out of order and
    a resumed run needs to know exactly which are already local and intact.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT notes FROM staging.ingest_state WHERE job = %s", (job,))
        row = cur.fetchone()
    if not row or not row[0] or not isinstance(row[0], dict):
        return set()
    return set(row[0].get("completed", []))


def add_marker(conn: psycopg.Connection, job: str, item: str) -> None:
    done = get_marker(conn, job)
    done.add(item)
    with conn.cursor() as cur:
        cur.execute("SELECT notes FROM staging.ingest_state WHERE job = %s", (job,))
        row = cur.fetchone()
        notes = dict(row[0]) if row and isinstance(row[0], dict) else {}
        notes["completed"] = sorted(done)
        cur.execute(
            """
            INSERT INTO staging.ingest_state (job, notes, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (job) DO UPDATE SET notes = EXCLUDED.notes, updated_at = now()
            """,
            (job, Jsonb(notes)),
        )
    conn.commit()


def clear_marker(conn: psycopg.Connection, job: str) -> None:
    """Forget a set-shaped bookmark, so a fresh (non-resumed) pass starts clean.

    Without this, a `--no-resume` pass would add its parts to the previous
    pass's completed set, and if it were interrupted, resuming would skip every
    part the *previous* pass finished.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE staging.ingest_state SET notes = notes - 'completed', updated_at = now() "
            "WHERE job = %s AND jsonb_typeof(notes) = 'object'",
            (job,),
        )
    conn.commit()
