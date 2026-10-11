"""Bucket 1 orchestration — the physician spine.

    pex run nppes --seed        monthly full file (~1 hour)
    pex run nppes --weekly      weekly incremental (minutes)
    pex run nppes --taxonomy    NUCC reference refresh only (seconds)
    pex run nppes --geocode     geocode pass only

**This bucket must load before any other.** The matchers in buckets 3 and 4 read
the spine as their input, and bucket 2 joins onto it — run any of them against an
empty spine and they insert nothing, silently.
"""

from __future__ import annotations

import logging

from ...config import Settings
from ...db import connect
from ...http import client_for
from ...runs import RunSummary
from ...spine import rebuild as rebuild_spine
from ...state import mark_finished, mark_started
from . import columns, geocode, sources, stage, taxonomy, transform

log = logging.getLogger("pex.nppes")


def load_taxonomy(settings: Settings) -> None:
    """Refresh the NUCC reference table. Must precede the physician load."""
    if settings.nucc_csv:
        # Supplied by hand via PEX_NUCC_CSV; nothing is fetched from nucc.org.
        path = settings.nucc_csv
        if not path.is_file():
            raise FileNotFoundError(f"PEX_NUCC_CSV points at {path}, which is not a file")
        log.info("NUCC taxonomy from PEX_NUCC_CSV: %s", path)
    else:
        http = client_for(settings, "nppes")
        url, version = sources.discover_nucc(http)
        path = http.download(url, settings.path("nucc", f"nucc_taxonomy_{version}.csv"))
    with connect(settings) as conn, RunSummary("nucc_taxonomy", conn) as run:
        run.add("codes", taxonomy.load(conn, path))


def run_geocode(settings: Settings, max_batches: int | None = None) -> None:
    http = client_for(settings, "census")
    with connect(settings) as conn, RunSummary("nppes_geocode", conn) as run:
        geocode.load_zip_centroids(conn, settings, client_for(settings, "census"))
        stats = geocode.run(conn, settings, http, run, max_batches=max_batches)
        for key, value in stats.items():
            run.add(key, value)


def ingest(settings: Settings, weekly: bool = False, skip_geocode: bool = False) -> None:
    """Download → extract → stage → transform → geocode.

    The monthly full file is a true-up: it catches anything a weekly missed and
    confirms deactivations. The weekly incremental runs the identical
    filter/transform/upsert over a much smaller file — which is exactly why they
    share this function rather than having two near-identical copies that drift.
    """
    kind = "weekly" if weekly else "monthly"
    job = f"nppes_{'weekly' if weekly else 'seed'}"

    http = client_for(settings, "nppes")
    available = sources.discover_nppes(http)
    if kind not in available:
        raise RuntimeError(f"No {kind} NPPES file found on the landing page")
    entry = available[kind]

    snapshot = settings.snapshot_dir("nppes", entry.release)
    zip_path = snapshot / entry.filename

    with connect(settings) as conn, RunSummary(job, conn) as run:
        mark_started(conn, job)
        run.note("release", entry.release)
        run.report_path = settings.report_dir / f"{job}-{entry.release}.json"

        # 1. Download (skipped when the local file is already complete) and extract.
        http.download(entry.url, zip_path)
        extract_dir = stage.extract(zip_path, snapshot / "extracted")
        csv_path = sources.main_csv_in(extract_dir)
        run.note("source_file", csv_path.name)

        # 2. Resolve the header before touching the body. A layout change fails
        #    here, loudly, instead of producing a plausible but wrong database.
        headers = stage.read_headers(csv_path)
        resolved = columns.resolve(headers)
        run.add("columns_resolved", len(resolved))

        # 3. Stage the filtered physician subset.
        staged = stage.stage(settings, csv_path, resolved)
        run.add("staged", staged)

        # 4. Transform into the serving tables.
        stats = transform.run(conn, run)
        for key, value in stats.items():
            run.add(key, value)

        for warning in stage.sanity_check(stats["physicians_active"], weekly=weekly):
            log.warning("SANITY: %s", warning)
            run.note("sanity_warning", warning)
        for warning in transform.post_load_checks(conn):
            log.warning("CHECK: %s", warning)

        # The matchers in buckets 3 and 4 read the spine through this index;
        # it is derived from `physician`, so it must follow every spine load.
        for key, value in rebuild_spine(conn, settings).items():
            run.add(f"spine_{key}", value)

        mark_finished(conn, job)

    # 5. Geocode — only new or address-changed practice locations.
    if not skip_geocode:
        run_geocode(settings)
