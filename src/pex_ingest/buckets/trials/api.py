"""ClinicalTrials.gov v2 API: the pager and the field list.

No key, no registration, no rate-limit tier — US federal public domain. The whole
world corpus is ~599k studies and downloads in about 13 minutes, which is why
this bucket pulls **everything** and matches locally.

Never use per-name search as the seeding path. It is loose text matching where
every hit is a candidate rather than a match, and per-physician searching would
be 2M+ calls to reproduce a corpus you can download in 13 minutes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

from ...state import mark_finished, load_cursor, save_cursor

log = logging.getLogger("pex.trials.api")

BASE = "https://clinicaltrials.gov/api/v2/studies"
STATS = "https://clinicaltrials.gov/api/v2/stats/size"
METADATA = "https://clinicaltrials.gov/api/v2/studies/metadata"

PAGE_SIZE = 1000

# The `fields=` parameter is the difference between ~10 GB and ~2 GB. Request
# only what we surface or match on. Field paths come from the API's own
# self-documenting /studies/metadata endpoint — check it rather than guessing,
# since names drift.
FIELDS = ",".join([
    "NCTId",
    "BriefTitle",
    "OfficialTitle",
    "BriefSummary",
    "Condition",
    "ConditionMeshTerm",
    "ConditionMeshId",
    "InterventionType",
    "InterventionName",
    "InterventionDescription",
    "InterventionMeshTerm",
    "InterventionMeshId",
    "OverallStatus",
    "Phase",
    "StartDate",
    "PrimaryCompletionDate",
    "OverallOfficialName",
    "OverallOfficialAffiliation",
    "OverallOfficialRole",
    "LocationFacility",
    "LocationCity",
    "LocationState",
    "LocationContactName",
    "LocationContactRole",
    "LastUpdatePostDate",
])


def corpus_size(http) -> int:
    data = http.get_json(STATS, use_cache=False)
    total = int(data.get("totalStudies", 0))
    log.info("ClinicalTrials.gov reports %s studies", f"{total:,}")
    return total


def walk(http, conn, job: str, extra_params: dict | None = None) -> Iterator[dict]:
    """Page through studies, persisting `nextPageToken` after every page.

    599 sequential calls is short enough to feel safe and long enough that a
    dropped connection at call 400 is annoying — hence the bookmark. Pacing is
    handled by the client (1.3s ≈ 46 req/min, under the ~50/min limit).
    """
    params = {"pageSize": PAGE_SIZE, "fields": FIELDS, **(extra_params or {})}
    token = load_cursor(conn, job)
    if token:
        log.info("%s resuming from a saved page token", job)

    pages = 0
    while True:
        page = http.get_json(
            BASE, params={**params, **({"pageToken": token} if token else {})}, use_cache=False
        )
        studies = page.get("studies", [])
        yield from studies
        pages += 1

        token = page.get("nextPageToken")
        save_cursor(conn, job, token)
        if not token:
            break
        if pages % 25 == 0:
            log.info("  … %d pages", pages)

    mark_finished(conn, job)
    log.info("%s complete after %d pages", job, pages)


def cache_jsonl(studies: Iterator[dict], path: Path) -> Iterator[dict]:
    """Tee the stream to a local JSONL cache as it passes through.

    "The local JSONL cache exists so a re-parse doesn't mean a re-pull."
    A slimming bug is then a local re-run, not another 13 minutes of API traffic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for study in studies:
            fh.write(json.dumps(study, separators=(",", ":")) + "\n")
            yield study


def read_jsonl(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)
