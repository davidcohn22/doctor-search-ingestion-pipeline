"""`pex` — the one entry point for every ingestion job.

    pex preflight                    check the workstation before anything runs
    pex migrate                      apply migrations/*.sql on top of the docs-repo DDL
    pex run nppes --seed             bucket 1 — the physician spine
    pex run cms                      bucket 2 — medical school
    pex run publications --all       bucket 3 — publications
    pex run trials --seed            bucket 4 — clinical trials
    pex run conditions --all         bucket 5 — condition vocabulary
    pex features                     rebuild the precomputed search features
    pex status                       what has run, when, and what it did

Bucket 1 must load before any other; 2, 3 and 4 are independent of each other;
5 finalizes after 3 (docs/prerequisites/README.md#run-order).

Every command reads its configuration from the environment, never from flags —
that is what makes the eventual move to hosted Postgres a config change rather
than a rewrite, and it is why there is no `--database-url` option here.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import REPO_ROOT, ConfigError, load_settings
from .runs import setup_logging

log = logging.getLogger("pex")


def _settings(require_db: bool = True):
    try:
        return load_settings(require_db=require_db)
    except ConfigError as exc:
        print(f"\nConfiguration error:\n\n{exc}\n", file=sys.stderr)
        raise SystemExit(2) from exc


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_preflight(args) -> int:
    """The four checks common to every bucket, plus the two people skip."""
    import shutil

    settings = _settings()
    ok = True

    print(f"python            {sys.version.split()[0]}")
    if sys.version_info < (3, 12):
        print("  ✗ want 3.12+ — see docs/prerequisites/00-workstation.md")
        ok = False

    for module in ("psycopg", "duckdb", "lxml", "rapidfuzz", "pyarrow", "requests", "yaml"):
        try:
            __import__(module)
        except ImportError:
            print(f"  ✗ missing dependency: {module}  (run: uv sync)")
            ok = False

    print(f"PEX_DATA_DIR      {settings.data_dir}")
    usage = shutil.disk_usage(settings.data_dir)
    free_gb = usage.free / 1e9
    print(f"  free space      {free_gb:,.0f} GB")
    if free_gb < 150:
        print("  ✗ want 150 GB+ free — the OpenAlex snapshot alone is ~106 GB")
        ok = False

    print(f"contact email     {settings.contact_email}")
    for name, label in (
        ("ncbi_api_key", "NCBI_API_KEY      (bucket 3)"),
        ("anthropic_api_key", "ANTHROPIC_API_KEY (bucket 5)"),
        ("openalex_api_key", "OPENALEX_API_KEY  (bucket 3, API path only)"),
    ):
        print(f"{label}: {'set' if getattr(settings, name) else 'unset'}")

    try:
        from .db import connect, scalar

        with connect(settings) as conn:
            user = scalar(conn, "SELECT current_user")
            tables = scalar(
                conn,
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema IN ('public', 'staging')",
            )
        print(f"database          connected as {user}, {tables} tables in public+staging")
        if tables < 21:
            print("  ✗ expected 21+ (16 serving + 5 staging) — load the docs-repo DDL first")
            ok = False
    except Exception as exc:
        print(f"database          ✗ {exc}")
        ok = False

    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        import subprocess

        ignored = subprocess.run(
            ["git", "check-ignore", "-q", ".env"], cwd=REPO_ROOT, capture_output=True
        )
        print(f".env              {'ignored by git ✓' if ignored.returncode == 0 else '✗ NOT IGNORED'}")
        if ignored.returncode != 0:
            print("  ✗ stop — the next commit would publish your database password")
            ok = False

    print("\npreflight", "passed" if ok else "FAILED")
    return 0 if ok else 1


def cmd_migrate(args) -> int:
    from .db import apply_migrations, connect

    settings = _settings()
    with connect(settings) as conn:
        applied = apply_migrations(conn, REPO_ROOT / "migrations")
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or 'none pending'}")
    return 0


def cmd_features(args) -> int:
    from .db import connect
    from .features import build
    from .runs import RunSummary

    settings = _settings()
    with connect(settings) as conn, RunSummary("features", conn) as run:
        for key, value in build.run(conn).items():
            run.add(key, value)
    return 0


def cmd_status(args) -> int:
    """What has run, when, and what it did — read straight from ingest_state."""
    from .db import connect, fetch_all

    settings = _settings()
    with connect(settings) as conn:
        rows = fetch_all(
            conn,
            """
            SELECT job, last_run_at, updated_at,
                   "cursor" IS NOT NULL AND "cursor" <> '__done__' AS in_progress,
                   notes -> 'latest' -> 'counters' AS counters,
                   notes -> 'latest' ->> 'elapsed_sec' AS elapsed
            FROM staging.ingest_state
            ORDER BY updated_at DESC
            """,
        )

    if not rows:
        print("no jobs have run yet")
        return 0

    print(f"{'job':<32} {'last completed':<22} {'state':<12} counters")
    print("-" * 100)
    for job, last_run, updated, in_progress, counters, elapsed in rows:
        state = "RESUMABLE" if in_progress else "idle"
        stamp = last_run.strftime("%Y-%m-%d %H:%M") if last_run else "never"
        summary = ""
        if counters:
            summary = " ".join(f"{k}={v:,}" if isinstance(v, int) else f"{k}={v}"
                               for k, v in list(counters.items())[:4])
            if elapsed:
                summary += f"  ({float(elapsed):.0f}s)"
        print(f"{job:<32} {stamp:<22} {state:<12} {summary}")
    return 0


def cmd_run(args) -> int:
    settings = _settings()
    bucket = args.bucket

    if bucket == "nppes":
        from .buckets.nppes import job

        if args.taxonomy:
            job.load_taxonomy(settings)
        elif args.geocode:
            job.run_geocode(settings, max_batches=args.max_batches)
        else:
            # The NUCC reference table has a foreign key from physician_taxonomy,
            # so it has to exist before the spine loads.
            job.load_taxonomy(settings)
            job.ingest(settings, weekly=args.weekly, skip_geocode=args.skip_geocode)

    elif bucket == "cms":
        from .buckets.cms_dac import job as cms_job

        cms_job.ingest(settings)

    elif bucket == "publications":
        from .buckets.publications import job as pub_job

        if args.all:
            pub_job.run_all(settings)
        if args.reference:
            pub_job.load_reference(settings)
        if args.pool:
            pub_job.build_pool(settings, resume=not args.no_resume)
        if args.match:
            pub_job.match(settings, use_orcid=not args.no_orcid)
        if args.works:
            pub_job.pull_works(settings, resume=not args.no_resume)
        if args.backfill_edges:
            pub_job.backfill_edges(settings)
        if args.mesh:
            pub_job.enrich_mesh(settings, limit=args.limit)
        if args.recheck:
            pub_job.enrich_mesh(settings, recheck=True, limit=args.limit)
        if args.delta:
            pub_job.delta(settings)
        if args.search_fallback:
            pub_job.search_fallback(settings, limit=args.limit or 500)

    elif bucket == "trials":
        from .buckets.trials import job as trial_job

        if args.match_only:
            trial_job.rematch(settings)
        else:
            trial_job.load(settings, delta=args.delta, match=not args.no_match)

    elif bucket == "conditions":
        from .buckets.conditions import job as cond_job

        if args.all:
            cond_job.run_all(settings)
        if args.parse:
            cond_job.parse(settings)
        if args.prune:
            cond_job.prune_candidates(settings)
        if args.enrich:
            cond_job.enrich_and_load(settings, live=args.live, limit=args.limit)
        if args.specialties:
            cond_job.map_specialties(settings)
        if args.review:
            cond_job.review(settings, limit=args.limit or 400)
        if args.promote:
            path = Path(args.promote)
            if not path.exists():
                path = settings.report_dir / args.promote
            cond_job.promote(settings, path, per_row=args.per_row)

    return 0


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pex",
        description="Physician Expertise Explorer — data ingestion pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="check the workstation").set_defaults(func=cmd_preflight)
    sub.add_parser("migrate", help="apply migrations/*.sql").set_defaults(func=cmd_migrate)
    sub.add_parser("features", help="rebuild search features").set_defaults(func=cmd_features)
    sub.add_parser("status", help="what has run and what it did").set_defaults(func=cmd_status)

    run = sub.add_parser("run", help="run an ingestion bucket")
    run.set_defaults(func=cmd_run)
    buckets = run.add_subparsers(dest="bucket", required=True)

    nppes = buckets.add_parser("nppes", help="bucket 1 — the physician spine")
    nppes.add_argument("--seed", action="store_true", help="monthly full file (default)")
    nppes.add_argument("--weekly", action="store_true", help="weekly incremental file")
    nppes.add_argument("--taxonomy", action="store_true", help="NUCC reference refresh only")
    nppes.add_argument("--geocode", action="store_true", help="geocoding pass only")
    nppes.add_argument("--skip-geocode", action="store_true", help="skip geocoding after load")
    nppes.add_argument("--max-batches", type=int, help="cap geocode batches (for testing)")

    buckets.add_parser("cms", help="bucket 2 — medical school")

    pubs = buckets.add_parser("publications", help="bucket 3 — publications")
    pubs.add_argument("--all", action="store_true", help="reference→pool→match→works→mesh")
    pubs.add_argument("--reference", action="store_true", help="institutions, topics, sources")
    pubs.add_argument("--pool", action="store_true", help="build the author pool")
    pubs.add_argument("--match", action="store_true", help="physician ↔ author matching")
    pubs.add_argument("--works", action="store_true", help="works pass over the snapshot")
    pubs.add_argument("--backfill-edges", action="store_true",
                      help="attach local works to newly matched physicians")
    pubs.add_argument("--mesh", action="store_true", help="PubMed MeSH backfill")
    pubs.add_argument("--recheck", action="store_true", help="monthly MeSH re-check sweep")
    pubs.add_argument("--delta", action="store_true", help="weekly API delta")
    pubs.add_argument("--search-fallback", action="store_true",
                      help="per-physician author search for the unmatched")
    pubs.add_argument("--no-orcid", action="store_true", help="skip ORCID confirmation calls")
    pubs.add_argument("--no-resume", action="store_true", help="ignore saved part-file progress")
    pubs.add_argument("--limit", type=int, help="cap the work item count (for testing)")

    trials = buckets.add_parser("trials", help="bucket 4 — clinical trials")
    trials.add_argument("--seed", action="store_true", help="full corpus (default)")
    trials.add_argument("--delta", action="store_true", help="weekly changed studies")
    trials.add_argument("--match-only", action="store_true", help="re-match without re-pulling")
    trials.add_argument("--no-match", action="store_true", help="pull without matching")

    conditions = buckets.add_parser("conditions", help="bucket 5 — condition vocabulary")
    conditions.add_argument("--all", action="store_true", help="parse→prune→enrich→specialties→review")
    conditions.add_argument("--parse", action="store_true", help="download + parse MeSH")
    conditions.add_argument("--prune", action="store_true", help="apply publication thresholds")
    conditions.add_argument("--enrich", action="store_true", help="LLM pass + draft load")
    conditions.add_argument("--specialties", action="store_true", help="derive condition_taxonomy")
    conditions.add_argument("--review", action="store_true", help="write a QA review sheet")
    conditions.add_argument("--promote", metavar="FILE", help="apply a reviewed sheet")
    conditions.add_argument("--per-row", action="store_true",
                            help="promote only reviewed rows, not their whole branch")
    conditions.add_argument("--live", action="store_true",
                            help="live API calls instead of the Batches API (samples only)")
    conditions.add_argument("--limit", type=int, help="cap the descriptor count (for testing)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings_log_level = "DEBUG" if args.verbose else None
    try:
        level = settings_log_level or load_settings(require_db=False).log_level
        log_dir = load_settings(require_db=False).report_dir / "logs"
    except ConfigError:
        level, log_dir = settings_log_level or "INFO", None
    setup_logging(level, log_dir)

    try:
        return args.func(args)
    except KeyboardInterrupt:
        # Every long walk persists its position after each batch, so an
        # interrupted job resumes rather than restarts.
        log.warning("interrupted — progress is saved; re-run the same command to resume")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
