"""Bucket 5 orchestration — condition vocabulary.

    pex run conditions --parse        download + parse MeSH into candidates
    pex run conditions --prune        count papers per descriptor, apply thresholds
    pex run conditions --enrich       LLM batch pass + load draft conditions
    pex run conditions --specialties  derive condition_taxonomy from publishing
    pex run conditions --review       write a stratified QA review sheet
    pex run conditions --promote FILE apply a reviewed sheet's verdicts
    pex run conditions --all          parse → prune → enrich → specialties → review

Runs last, and splits in two: the MeSH parse can run any time, but the pruning
step and the specialty mapping both need works in the database, so the vocabulary
*finalizes* after bucket 3.

This is the only bucket that spends money (~$7 nationally) and the only one whose
output a user reads as product copy — hence the QA gate at the end.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...config import Settings
from ...db import connect
from ...http import client_for
from ...runs import RunSummary
from . import branch_config, enrich, load, medlineplus, mesh_parse, prune, qa, sources
from . import taxonomy_map

log = logging.getLogger("pex.conditions")


def parse(settings: Settings, year: int | None = None) -> None:
    config = branch_config.load()
    http = client_for(settings, "nlm")

    with connect(settings) as conn, RunSummary("mesh_parse", conn) as run:
        path, resolved_year = sources.download_mesh(http, settings, year)
        run.note("mesh_year", resolved_year)
        for key, value in mesh_parse.load(conn, path, config).items():
            run.add(key, value)


def prune_candidates(settings: Settings) -> list[dict]:
    config = branch_config.load()
    with connect(settings) as conn, RunSummary("mesh_prune", conn) as run:
        for key, value in prune.rebuild_counts(conn).items():
            run.add(key, value)
        rows = prune.survivors(conn, config)
        run.add("survivors", len(rows))
        return rows


def enrich_and_load(settings: Settings, live: bool = False, limit: int | None = None) -> None:
    """The LLM pass, then the draft load. The one step that costs money."""
    config = branch_config.load()
    http = client_for(settings, "nlm")

    with connect(settings) as conn, RunSummary("condition_enrich", conn) as run:
        run.report_path = settings.report_dir / "condition_enrich.json"

        survivors = prune.survivors(conn, config)
        if limit:
            survivors = survivors[:limit]
        if not survivors:
            log.error(
                "no descriptors survived the prune — has bucket 3 been loaded? "
                "The prune keeps only conditions the literature writes about."
            )
            return
        run.add("survivors", len(survivors))

        # MedlinePlus first: every descriptor it covers is one the LLM doesn't
        # have to name, and its naming is human-authored and authoritative.
        year = sources.resolve_mesh_year(http)
        topics_path = sources.download_medlineplus(http, settings, year)
        crosswalk = medlineplus.parse(topics_path)
        covered = sum(1 for s in survivors if s["mesh_id"] in crosswalk)
        run.add("medlineplus_covered", covered)
        log.info("MedlinePlus names %s of %s survivors", f"{covered:,}", f"{len(survivors):,}")

        enrich.estimate_cost(survivors, config, settings)
        enrichment = (
            enrich.run_live(survivors, config, settings)
            if live
            else enrich.run_batch(survivors, config, settings)
        )
        run.add("enriched", len(enrichment))

        for key, value in load.run(conn, survivors, crosswalk, enrichment).items():
            run.add(key, value)
        for warning in load.post_load_checks(conn):
            log.warning("CHECK: %s", warning)
            run.note("check_warning", warning)


def map_specialties(settings: Settings) -> None:
    config = branch_config.load()
    with connect(settings) as conn, RunSummary("condition_taxonomy", conn) as run:
        for key, value in taxonomy_map.run(conn, config).items():
            run.add(key, value)


def review(settings: Settings, limit: int = 400) -> Path:
    with connect(settings) as conn:
        return qa.review_sheet(conn, settings.report_dir, limit=limit)


def promote(settings: Settings, sheet: Path, per_row: bool = False) -> None:
    with connect(settings) as conn, RunSummary("condition_promote", conn) as run:
        for key, value in qa.promote(conn, sheet, promote_branch=not per_row).items():
            run.add(key, value)


def run_all(settings: Settings) -> None:
    parse(settings)
    prune_candidates(settings)
    enrich_and_load(settings)
    map_specialties(settings)
    review(settings)
