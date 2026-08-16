"""Bucket 3 orchestration — publications.

    pex run publications --reference    institutions, topics, sources (do this first)
    pex run publications --pool         build the 11.2M-row author pool
    pex run publications --match        physician ↔ author matching (free, re-runnable)
    pex run publications --works        the projected snapshot pass over works
    pex run publications --mesh         PubMed MeSH backfill
    pex run publications --recheck      monthly MeSH re-check sweep
    pex run publications --delta        weekly API delta for matched authors
    pex run publications --all          reference → pool → match → works → mesh

The largest, slowest and highest-risk bucket. It follows
docs/decisions/0001-openalex-works-via-snapshot.md: **snapshot for the one-time
backfill, API for ongoing freshness.**

Step order is not stylistic. The reference sets must exist before matching (the
geography signal resolves institution IDs through them), matching must precede
the works pass (works are pulled for matched authors only), and MeSH enrichment
follows works because it operates on the PMIDs those works carry.
"""

from __future__ import annotations

import logging

from ...config import Settings
from ...db import connect
from ...http import client_for
from ...runs import RunSummary
from ...spine import rebuild as rebuild_spine
from . import author_pool, deltas, matcher, mesh, reference, works

log = logging.getLogger("pex.publications")


def load_reference(settings: Settings) -> None:
    http = client_for(settings, "s3")
    with connect(settings) as conn, RunSummary("openalex_reference", conn) as run:
        for entity, count in reference.load(conn, settings, http).items():
            run.add(entity, count)


def build_pool(settings: Settings, resume: bool = True) -> None:
    http = client_for(settings, "s3")
    with connect(settings) as conn, RunSummary("openalex_author_pool", conn) as run:
        run.report_path = settings.report_dir / "openalex_author_pool.json"
        for key, value in author_pool.build(conn, settings, http, resume=resume).items():
            run.add(key, value)
        for warning in author_pool.sanity_checks(conn):
            log.warning("SANITY: %s", warning)
            run.note("sanity_warning", warning)


def match(settings: Settings, rebuild_index: bool = True, use_orcid: bool = True) -> None:
    """Run the matcher. Free and local, so re-run it after any scoring change."""
    with connect(settings) as conn, RunSummary("openalex_match", conn) as run:
        run.report_path = settings.report_dir / "openalex_match.json"
        if rebuild_index:
            # The name index is derived from the spine, and the spine moves
            # weekly — a stale index silently costs recall on new physicians.
            for key, value in rebuild_spine(conn, settings).items():
                run.add(f"spine_{key}", value)
        for key, value in matcher.run(conn, settings, use_orcid=use_orcid).items():
            run.add(key, value)
        run.note("coverage", matcher.coverage(conn))


def pull_works(settings: Settings, keep_edges: bool = True, resume: bool = True) -> None:
    http = client_for(settings, "s3")
    with connect(settings) as conn, RunSummary("openalex_works", conn) as run:
        run.report_path = settings.report_dir / "openalex_works.json"
        for key, value in works.build(
            conn, settings, http, keep_edges=keep_edges, resume=resume
        ).items():
            run.add(key, value)


def backfill_edges(settings: Settings) -> None:
    """Attach already-local works to newly matched physicians. Costs nothing."""
    with connect(settings) as conn, RunSummary("openalex_edge_backfill", conn) as run:
        run.add("authorships", works.backfill_from_edges(conn))


def enrich_mesh(settings: Settings, recheck: bool = False, limit: int | None = None) -> None:
    job = "pubmed_mesh_recheck" if recheck else "pubmed_mesh_backfill"
    with connect(settings) as conn, RunSummary(job, conn) as run:
        for key, value in mesh.run(conn, settings, recheck=recheck, limit=limit).items():
            run.add(key, value)
        run.note("coverage_by_year", mesh.coverage(conn))


def delta(settings: Settings) -> None:
    with connect(settings) as conn, RunSummary("openalex_delta", conn) as run:
        for key, value in deltas.run(conn, settings).items():
            run.add(key, value)


def search_fallback(settings: Settings, limit: int = 500) -> None:
    with connect(settings) as conn, RunSummary("openalex_search_fallback", conn) as run:
        for key, value in deltas.search_fallback(conn, settings, limit=limit).items():
            run.add(key, value)


def run_all(settings: Settings) -> None:
    """The full backfill, in dependency order. Hours, not months."""
    load_reference(settings)
    build_pool(settings)
    match(settings)
    pull_works(settings)
    enrich_mesh(settings)
