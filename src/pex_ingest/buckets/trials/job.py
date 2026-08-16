"""Bucket 4 orchestration — clinical trials.

    pex run trials --seed       full corpus (~599k studies, ~13 minutes)
    pex run trials --delta      weekly changed studies (minutes)
    pex run trials --match      re-match without re-pulling

The best cost-to-value ratio of the five buckets: no credentials, no bulk file,
and the entire world corpus in about thirteen minutes. Bucket 1 must be loaded
first — this bucket matches names against the spine.
"""

from __future__ import annotations

import logging
from datetime import date

import psycopg

from ...config import Settings
from ...db import Jsonb, analyze, connect
from ...http import client_for
from ...runs import RunSummary
from ...state import delta_since
from . import api, matcher
from .slim import slim_study

log = logging.getLogger("pex.trials")

UPSERT_TRIAL = """
INSERT INTO trial (nct_id, brief_title, official_title, brief_summary, overall_status,
                   phase, conditions, condition_mesh_ids, interventions,
                   intervention_mesh_ids, start_date, primary_completion_date,
                   raw, retrieved_at)
VALUES (%(nct_id)s, %(brief_title)s, %(official_title)s, %(brief_summary)s,
        %(overall_status)s, %(phase)s, %(conditions)s, %(condition_mesh_ids)s,
        %(interventions)s, %(intervention_mesh_ids)s, %(start_date)s,
        %(primary_completion_date)s, %(raw)s, now())
ON CONFLICT (nct_id) DO UPDATE SET
    brief_title             = EXCLUDED.brief_title,
    official_title          = EXCLUDED.official_title,
    brief_summary           = EXCLUDED.brief_summary,
    overall_status          = EXCLUDED.overall_status,
    phase                   = EXCLUDED.phase,
    conditions              = EXCLUDED.conditions,
    condition_mesh_ids      = EXCLUDED.condition_mesh_ids,
    interventions           = EXCLUDED.interventions,
    intervention_mesh_ids   = EXCLUDED.intervention_mesh_ids,
    start_date              = EXCLUDED.start_date,
    primary_completion_date = EXCLUDED.primary_completion_date,
    raw                     = EXCLUDED.raw,
    retrieved_at            = now()
"""

INSERT_MENTION = """
INSERT INTO staging.trial_person_index
    (name, nct_id, source_field, role, affiliation, facility, city, state, name_key)
VALUES (%(name)s, %(nct_id)s, %(source_field)s, %(role)s, %(affiliation)s,
        %(facility)s, %(city)s, %(state)s, %(name_key)s)
"""


def _to_row(trial: dict) -> dict:
    """Adapt a slimmed trial into psycopg parameters (JSON columns wrapped)."""
    return {
        **trial,
        "conditions": Jsonb(trial["conditions"]),
        "condition_mesh_ids": Jsonb(trial["condition_mesh_ids"]),
        "interventions": Jsonb(trial["interventions"]),
        "intervention_mesh_ids": Jsonb(trial["intervention_mesh_ids"]),
        "raw": Jsonb({"last_update_posted": trial.get("last_update_posted")}),
    }


def load(settings: Settings, delta: bool = False, match: bool = True) -> None:
    """Pull studies, replace their person mentions, then re-match those trials."""
    job = "ctgov_delta" if delta else "ctgov_seed"
    http = client_for(settings, "ctgov")
    release = date.today().strftime("%Y-%m")

    with connect(settings) as conn, RunSummary(job, conn) as run:
        run.report_path = settings.report_dir / f"{job}-{date.today().isoformat()}.json"

        extra: dict = {}
        if delta:
            since = delta_since(conn, "ctgov_seed")
            extra = {"query.term": f"AREA[LastUpdatePostDate]RANGE[{since},MAX]"}
            log.info("delta: studies changed since %s", since)
            run.note("since", since)
        else:
            run.note("corpus_size", api.corpus_size(http))

        cache_path = settings.snapshot_dir("ctgov", release) / f"{job}.jsonl"
        studies = api.cache_jsonl(api.walk(http, conn, job, extra), cache_path)

        touched: list[str] = []
        trials: list[dict] = []
        mentions: list[dict] = []

        for study in studies:
            trial, study_mentions = slim_study(study)
            if not trial:
                run.reject("study_without_nct_id")
                continue
            trials.append(_to_row(trial))
            mentions.extend(study_mentions)
            touched.append(trial["nct_id"])
            run.add("studies", 1)

            if len(trials) >= settings.batch_size:
                _write(conn, trials, mentions, run)
                trials, mentions = [], []

        _write(conn, trials, mentions, run)
        analyze(conn, "trial", "staging.trial_person_index")

        run.add("trials_touched", len(touched))
        _mention_balance(conn, run)

    if match and touched:
        rematch(settings, nct_ids=touched if delta else None)


def _write(conn: psycopg.Connection, trials: list[dict], mentions: list[dict], run) -> None:
    """Upsert trials and *replace* their person mentions, in one transaction.

    Delete-then-insert rather than diffing two mention lists: simpler, less
    bug-prone, and the whole exchange has to be atomic — a crash between the
    delete and the insert would leave a trial with no named people at all, which
    looks exactly like a trial that never had any.
    """
    if not trials:
        return
    nct_ids = [t["nct_id"] for t in trials]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM staging.trial_person_index WHERE nct_id = ANY(%s)", (nct_ids,))
        cur.executemany(UPSERT_TRIAL, trials)
        if mentions:
            cur.executemany(INSERT_MENTION, mentions)
    conn.commit()
    run.add("trials_written", len(trials))
    run.add("mentions_written", len(mentions))


def _mention_balance(conn: psycopg.Connection, run) -> None:
    """Site contacts should dominate overall officials.

    "If overall officials dominate, `locations[].contacts` isn't being
    traversed" — the single most likely extraction bug in this bucket, and one
    that would silently cost most of its coverage.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_field, count(*) FROM staging.trial_person_index GROUP BY 1"
        )
        counts = dict(cur.fetchall())
    run.note("mentions_by_field", counts)
    site = counts.get("location_contact", 0)
    official = counts.get("overall_official", 0)
    log.info("mentions: %s site contacts, %s overall officials", f"{site:,}", f"{official:,}")
    if official and site < official:
        log.warning(
            "overall officials outnumber site contacts — locations[].contacts is "
            "probably not being traversed"
        )


def rematch(settings: Settings, nct_ids: list[str] | None = None) -> None:
    with connect(settings) as conn, RunSummary("ctgov_match", conn) as run:
        stats = matcher.run(conn, settings, nct_ids=nct_ids)
        for key, value in stats.items():
            run.add(key, value)
        analyze(conn, "trial_investigator")
