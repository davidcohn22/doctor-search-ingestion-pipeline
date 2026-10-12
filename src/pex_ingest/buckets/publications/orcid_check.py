"""ORCID check: confirm, reject or promote author matches from the owner's own record.

An ORCID record is written by the researcher (or their institution) and lists
their employers, education and papers. For a physician ↔ OpenAlex author link
that carries an ORCID, the record is the best evidence available about whether
the link is the same human:

  * **confirmed** — the record names an organization in the physician's practice
    state. The link stops being a name-similarity guess.
  * **foreign**   — the record lists affiliations with countries, and none is in
    the US. The profile belongs to a researcher abroad who shares the name: the
    "Charles Bernstein, Winnipeg" case behind the #1 IBD result (2026-10-11).
  * **other_state** / **no_info** — US affiliations elsewhere (doctors move), or
    nothing usable. Neither changes a decision.

Two passes, so that the slow part runs once and the cheap part can be re-run:

    pex run publications --orcid-fetch    fetch records (network, hours, resumable)
    pex run publications --orcid-apply    apply verdicts to physician_author (seconds)

The fetch covers every ORCID on an accepted match plus held matches scoring at
least HELD_MIN_CONFIDENCE, where a confirmation is enough to promote. It writes
`staging.orcid_record` and keeps the older `orcid_employment_cache` in step, so
the matcher's own ORCID signal reads the same evidence offline.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import psycopg
import requests

from ...config import Settings
from ...db import Jsonb, upsert
from ...us_states import to_code

log = logging.getLogger("pex.orcid.check")

BASE = "https://pub.orcid.org/v3.0"

# ORCID's public API allows 24 requests/s per IP (burst 40). Half of that keeps
# us polite and still fetches ~130K records in about three hours.
WORKERS = 6
PER_WORKER_INTERVAL = 0.5     # 6 workers × 2/s = 12 requests/s
HELD_MIN_CONFIDENCE = 0.80

AFFILIATION_SECTIONS = {
    "employments": "employment",
    "educations": "education",
    "qualifications": "qualification",
    "invited-positions": "invited-position",
}

UPSERT_RECORD = """
INSERT INTO staging.orcid_record (orcid, http_status, given_names, family_name,
                                  affiliations, dois, pmids, fetched_at)
VALUES (%(orcid)s, %(status)s, %(given)s, %(family)s, %(affiliations)s, %(dois)s,
        %(pmids)s, now())
ON CONFLICT (orcid) DO UPDATE SET
    http_status = EXCLUDED.http_status, given_names = EXCLUDED.given_names,
    family_name = EXCLUDED.family_name, affiliations = EXCLUDED.affiliations,
    dois = EXCLUDED.dois, pmids = EXCLUDED.pmids, fetched_at = now()
"""

UPSERT_EMPLOYMENT_CACHE = """
INSERT INTO staging.orcid_employment_cache (orcid, states, organizations, fetched_at)
VALUES (%(orcid)s, %(states)s, %(organizations)s, now())
ON CONFLICT (orcid) DO UPDATE SET
    states = EXCLUDED.states, organizations = EXCLUDED.organizations, fetched_at = now()
"""

TODO_ORCIDS = """
SELECT orcid
FROM physician_author pa
WHERE orcid IS NOT NULL
  AND (status = 'accepted' OR (status = 'held' AND confidence >= %(held_min)s))
  AND NOT EXISTS (SELECT 1 FROM staging.orcid_record r WHERE r.orcid = pa.orcid)
GROUP BY orcid
-- Accepted matches first: they are the ones the plan requires; held ones are a
-- recall bonus and can be cut short if time runs out.
ORDER BY bool_or(status = 'accepted') DESC, max(confidence) DESC
"""


def _value(node, *path):
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _year(date_node) -> int | None:
    year = _value(date_node, "year", "value")
    try:
        return int(year) if year else None
    except ValueError:
        return None


def parse_record(data: dict) -> dict:
    """Reduce a /record JSON document to names, affiliations and work ids."""
    activities = data.get("activities-summary") or {}
    affiliations = []
    for section, kind in AFFILIATION_SECTIONS.items():
        for group in _value(activities, section, "affiliation-group") or []:
            for summary in group.get("summaries") or []:
                item = summary.get(f"{kind}-summary") or {}
                organization = item.get("organization") or {}
                address = organization.get("address") or {}
                country = (address.get("country") or "").upper() or None
                affiliations.append({
                    "type": kind,
                    "org": organization.get("name"),
                    "country": country,
                    "state": to_code(address.get("region")) if country == "US" else None,
                    "start": _year(item.get("start-date")),
                    "end": _year(item.get("end-date")),
                })

    dois, pmids = set(), set()
    for group in _value(activities, "works", "group") or []:
        for ext in _value(group, "external-ids", "external-id") or []:
            kind = (ext.get("external-id-type") or "").lower()
            value = (_value(ext, "external-id-normalized", "value")
                     or ext.get("external-id-value") or "").strip()
            if not value:
                continue
            if kind == "doi":
                dois.add(value.lower().removeprefix("https://doi.org/"))
            elif kind == "pmid":
                pmids.add(value.rstrip("/").rsplit("/", 1)[-1])

    name = _value(data, "person", "name") or {}
    return {
        "given": _value(name, "given-names", "value"),
        "family": _value(name, "family-name", "value"),
        "affiliations": affiliations,
        "dois": sorted(dois),
        "pmids": sorted(pmids),
    }


def first_names_agree(orcid_given: str | None, physician_first: str | None) -> bool | None:
    """Does the ORCID owner's first name fit the physician's? None when unknown.

    Initials and prefixes agree ("J." / "John", "Jon" / "Jonathan"); a different
    first name means the ORCID — and so this OpenAlex profile, or at least the part
    carrying the ORCID — belongs to someone else.
    """
    import re

    def first(text):
        tokens = [t for t in re.split(r"[^a-z]+", (text or "").lower()) if t]
        return tokens[0] if tokens else ""

    given, mine = first(orcid_given), first(physician_first)
    if not given or not mine:
        return None
    return given.startswith(mine) or mine.startswith(given)


def verdict(affiliations: list[dict], practice_state: str | None,
            orcid_given: str | None = None, physician_first: str | None = None) -> str:
    """'confirmed' | 'foreign' | 'other_state' | 'no_info' | 'name_mismatch'.

    A same-state namesake is exactly what ORCID can't separate by place, so the
    owner's first name has to agree before an employer counts as confirmation
    (added 2026-10-11, after same-state namesakes were promoted on one ORCID).
    """
    if first_names_agree(orcid_given, physician_first) is False:
        return "name_mismatch"
    countries = {a.get("country") for a in affiliations or [] if a.get("country")}
    if not countries:
        return "no_info"
    if "US" not in countries:
        return "foreign"
    states = {a.get("state") for a in affiliations if a.get("country") == "US"}
    if practice_state and practice_state.upper() in states:
        return "confirmed"
    return "other_state"


class _Fetcher:
    """One thread's session, paced independently."""

    def __init__(self, user_agent: str):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        self.last = 0.0

    def get(self, orcid: str) -> tuple[int, dict | None]:
        for attempt in range(6):
            gap = time.monotonic() - self.last
            if gap < PER_WORKER_INTERVAL:
                time.sleep(PER_WORKER_INTERVAL - gap)
            try:
                resp = self.session.get(f"{BASE}/{orcid}/record", timeout=60)
            except requests.RequestException as exc:
                self.last = time.monotonic()
                log.debug("ORCID %s: %s (attempt %d)", orcid, exc, attempt + 1)
                time.sleep(min(2 ** attempt, 60))
                continue
            self.last = time.monotonic()
            if resp.status_code == 200:
                return 200, resp.json()
            if resp.status_code in (404, 409, 410):
                # Deleted, locked or deprecated: a final answer, cached as such.
                return resp.status_code, None
            delay = float(resp.headers.get("Retry-After") or min(2 ** attempt, 60))
            log.warning("ORCID %s → HTTP %d; retrying in %.0fs", orcid, resp.status_code, delay)
            time.sleep(delay)
        return 0, None   # gave up; not stored, so the next run retries it


def fetch(conn: psycopg.Connection, settings: Settings, limit: int | None = None,
          workers: int = WORKERS) -> dict:
    """Fetch every ORCID record not yet stored. Resumable: stored ones are skipped."""
    with conn.cursor() as cur:
        cur.execute(TODO_ORCIDS, {"held_min": HELD_MIN_CONFIDENCE})
        todo = [row[0] for row in cur.fetchall()]
    if limit:
        todo = todo[:limit]
    log.info("ORCID fetch: %s records to fetch with %d workers", f"{len(todo):,}", workers)

    local = threading.local()

    def work(orcid: str) -> dict | None:
        if not hasattr(local, "fetcher"):
            local.fetcher = _Fetcher(settings.user_agent)
        status, data = local.fetcher.get(orcid)
        if status == 0:
            return None
        parsed = parse_record(data) if data else {
            "given": None, "family": None, "affiliations": [], "dois": [], "pmids": []}
        return {"orcid": orcid, "status": status, **parsed}

    stats: defaultdict = defaultdict(int)
    started = time.monotonic()
    batch: list[dict] = []

    def flush() -> None:
        if not batch:
            return
        upsert(conn, UPSERT_RECORD, [{
            "orcid": r["orcid"], "status": r["status"], "given": r["given"],
            "family": r["family"], "affiliations": Jsonb(r["affiliations"]),
            "dois": Jsonb(r["dois"]), "pmids": Jsonb(r["pmids"]),
        } for r in batch])
        upsert(conn, UPSERT_EMPLOYMENT_CACHE, [{
            "orcid": r["orcid"],
            "states": Jsonb(sorted({a["state"] for a in r["affiliations"]
                                    if a["type"] == "employment" and a["state"]})),
            "organizations": Jsonb([a["org"] for a in r["affiliations"]
                                    if a["type"] == "employment" and a["org"]][:20]),
        } for r in batch if r["status"] == 200])
        batch.clear()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(work, todo):
            if result is None:
                stats["failed"] += 1
                continue
            stats[f"http_{result['status']}"] += 1
            stats["fetched"] += 1
            batch.append(result)
            if len(batch) >= 500:
                flush()
            if stats["fetched"] % 5_000 == 0:
                rate = stats["fetched"] / (time.monotonic() - started)
                log.info("  … %s/%s ORCID records (%.1f/s, %s failed)",
                         f"{stats['fetched']:,}", f"{len(todo):,}", rate,
                         f"{stats['failed']:,}")
    flush()
    log.info("ORCID fetch done: %s", json.dumps(dict(stats)))
    return dict(stats)


VERDICT_ROWS = """
SELECT pa.npi, pa.openalex_author_id, pa.status, pa.confidence, pa.signals,
       f.practice_state, r.affiliations, r.given_names, f.first_name
FROM physician_author pa
JOIN staging.orcid_record r ON r.orcid = pa.orcid AND r.http_status = 200
JOIN staging.physician_match_facts f ON f.npi = pa.npi
WHERE pa.status IN ('accepted', 'held')
"""

# Held pairs whose own signals say "different person" stay held even when an
# ORCID confirms the state: a namesake in the same state is exactly the case
# ORCID can't separate.
BLOCKING_SIGNALS = ("first_name_mismatch", "middle_conflict", "probable_merged_entity")

APPLY = """
UPDATE physician_author SET
    status          = %(status)s,
    confidence      = %(confidence)s,
    match_method    = CASE WHEN %(promote_method)s THEN 'orcid_confirmed' ELSE match_method END,
    orcid_confirmed = %(confirmed)s,
    signals         = coalesce(signals, '{}'::jsonb) || %(extra)s
WHERE npi = %(npi)s AND openalex_author_id = %(author_id)s AND status <> 'rejected'
"""


def apply(conn: psycopg.Connection) -> dict:
    """Turn stored ORCID records into match decisions. Cheap; safe to re-run."""
    from .matcher import demote_shared_authors_in_db

    stats: defaultdict = defaultdict(int)
    updates = []
    with conn.cursor() as cur:
        cur.execute(VERDICT_ROWS)
        rows = cur.fetchall()
        cur.execute("SELECT openalex_author_id, npi FROM physician_author "
                    "WHERE status = 'accepted'")
        accepted_for: dict[str, set] = defaultdict(set)
        for author_id, npi in cur.fetchall():
            accepted_for[author_id].add(npi)

    # A held pair is only promoted when nobody else holds the author: not already
    # accepted for another doctor, and not confirmed for two held doctors at once.
    confirmed_held: dict[str, set] = defaultdict(set)
    for npi, author_id, status, _c, _s, state, affiliations, given, first in rows:
        if status == "held" and verdict(affiliations, state, given, first) == "confirmed":
            confirmed_held[author_id].add(npi)

    for npi, author_id, status, confidence, signals, state, affiliations, given, first in rows:
        signals = signals or {}
        result = verdict(affiliations, state, given, first)
        stats[f"{status}_{result}"] += 1
        new_status, new_conf = status, float(confidence)
        confirmed = False
        if result == "confirmed":
            confirmed = True
            if status == "held":
                if any(signals.get(s) for s in BLOCKING_SIGNALS) or signals.get(
                        "shared_author_demoted"):
                    stats["held_confirmed_blocked"] += 1
                elif accepted_for[author_id] - {npi} or len(confirmed_held[author_id]) > 1:
                    stats["held_confirmed_author_taken"] += 1
                else:
                    new_status = "accepted"
                    stats["promoted"] += 1
            if new_status == "accepted":
                new_conf = max(new_conf, 0.97)
        elif result == "foreign" and status == "accepted":
            new_status = "held"
            stats["rejected_foreign"] += 1

        updates.append({
            "npi": npi, "author_id": author_id, "status": new_status,
            "confidence": round(new_conf, 4),
            "promote_method": confirmed and new_status == "accepted",
            "confirmed": confirmed,
            "extra": Jsonb({"orcid_check": result}),
        })

    upsert(conn, APPLY, updates)
    stats["demoted_shared_author"] = demote_shared_authors_in_db(conn)
    log.info("ORCID apply: %s", json.dumps(dict(stats)))
    return dict(stats)
