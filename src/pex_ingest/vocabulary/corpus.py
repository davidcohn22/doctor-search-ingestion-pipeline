"""Run a TermIndex over every paper title (and trial text) using all CPU cores.

Shared by the literature check (which counts hits per term and per concept) and
the feature build (which needs every (paper, concept, tier) hit). Titles are read
from Postgres with a server-side cursor in chunks and matched in worker
processes; each worker receives the index once, at start-up.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from multiprocessing import get_context

import psycopg

from .titles import TermIndex

log = logging.getLogger("pex.vocabulary.corpus")

CHUNK = 20_000

_INDEX: TermIndex | None = None


def _init(index: TermIndex) -> None:
    global _INDEX
    _INDEX = index


def _match_chunk(rows: list[tuple[str, str]]) -> list[tuple[str, list[tuple[int, int, bool]]]]:
    out = []
    for key, text in rows:
        hits = _INDEX.match(text)
        if hits:
            out.append((key, [(cid, tier, name) for cid, (tier, name) in hits.items()]))
    return out


def _term_chunk(rows: list[tuple[str, str]]) -> list[tuple[str, list]]:
    out = []
    for key, text in rows:
        found = _INDEX.term_hits(text)
        if found:
            out.append((key, list(found)))
    return out


def _chunks(conn: psycopg.Connection, query: str) -> Iterator[list[tuple[str, str]]]:
    with conn.cursor(name="corpus_titles") as cur:
        cur.itersize = CHUNK
        cur.execute(query)
        batch = []
        for row in cur:
            batch.append(row)
            if len(batch) >= CHUNK:
                yield batch
                batch = []
        if batch:
            yield batch


def _run(conn, query: str, index: TermIndex, fn, workers: int | None):
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    ctx = get_context("fork")
    done = 0
    with ctx.Pool(workers, initializer=_init, initargs=(index,)) as pool:
        for result in pool.imap(fn, _chunks(conn, query), chunksize=1):
            done += 1
            if done % 50 == 0:
                log.info("  … %s rows matched", f"{done * CHUNK:,}")
            yield from result


WORK_TITLES = "SELECT openalex_work_id, title FROM work"

# A trial is matched on everything that names what it studies: both titles, the
# listed conditions and the interventions. Joined with " | " so a phrase can't
# span two fields ("|" is a word break).
TRIAL_TEXT = """
SELECT nct_id,
       concat_ws(' | ', brief_title, official_title,
                 (SELECT string_agg(value, ' | ') FROM jsonb_array_elements_text(conditions)),
                 (SELECT string_agg(coalesce(i->>'name', i #>> '{}'), ' | ')
                  FROM jsonb_array_elements(interventions) i))
FROM trial
"""


def match_works(conn, index: TermIndex, workers: int | None = None):
    """Yield (work_id, [(concept_id, tier, name_hit), …]) for every matching title."""
    yield from _run(conn, WORK_TITLES, index, _match_chunk, workers)


def match_trials(conn, index: TermIndex, workers: int | None = None):
    yield from _run(conn, TRIAL_TEXT, index, _match_chunk, workers)


def term_hits_in_works(conn, index: TermIndex, workers: int | None = None):
    """Yield (work_id, [term key, …]) for every title containing a registered term."""
    yield from _run(conn, WORK_TITLES, index, _term_chunk, workers)
