"""Weekly freshness for publications, via the OpenAlex REST API.

Decision 0001's recommendation is a **hybrid**, and this is its second half:

  * the free snapshot is **quarterly**, so a snapshot-only pipeline leaves a
    physician's newest papers up to three months stale — unacceptable for a
    product whose core claim is current evidence of expertise;
  * the API is **priced per call**, which is ruinous for 510M records and
    trivial for the ~5,700 works that change in a given week.

So: backfill from the snapshot, stay current from the API. This module walks
`works?filter=authorships.author.id:<ids>,from_updated_date:<date>` for matched
authors only — the volume the free $1/day budget is actually well suited to.

The cursor walk is OpenAlex's sanctioned full-export path: `cursor=*` to start,
each response carrying an opaque `next_cursor` passed verbatim to the next call,
`null` when done. Unlike `page=N` offset paging it is deterministic,
non-overlapping, and doesn't cap at 10k records — and the cursor is persisted
after *every* batch so a dropped connection resumes rather than restarts.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import psycopg

from ...config import Settings
from ...db import analyze, upsert
from ...http import client_for
from ...names import name_key
from ...state import delta_since, load_cursor, mark_finished, save_cursor
from .snapshot import openalex_id
from .works import UPSERT_AUTHORSHIP, UPSERT_WORK, matched_authors, position_of, slim_work

log = logging.getLogger("pex.openalex.delta")

API = "https://api.openalex.org/works"
JOB = "openalex_works_delta"

# OpenAlex accepts an OR-list of author ids in one filter. 50 keeps the URL well
# inside limits while cutting the call count by the same factor.
AUTHORS_PER_CALL = 50
PER_PAGE = 100


def _walk(http, conn, settings: Settings, author_ids: list[str], since: str) -> Iterator[dict]:
    """Cursor-walk the works changed since `since` for one batch of authors."""
    state_key = f"{JOB}:{author_ids[0]}"
    params = {
        "filter": (
            "authorships.author.id:" + "|".join(author_ids) + f",from_updated_date:{since}"
        ),
        "per-page": PER_PAGE,
        "cursor": load_cursor(conn, state_key) or "*",
        "mailto": settings.contact_email,
    }
    if settings.openalex_api_key:
        params["api_key"] = settings.openalex_api_key

    while params["cursor"]:
        page = http.get_json(API, params=params, use_cache=False)
        yield from page.get("results", [])
        params["cursor"] = (page.get("meta") or {}).get("next_cursor")
        save_cursor(conn, state_key, params["cursor"])
    mark_finished(conn, state_key)


def run(conn: psycopg.Connection, settings: Settings, since: str | None = None) -> dict:
    """Refresh works for every matched author changed since the last run."""
    http = client_for(settings, "openalex")
    authors = matched_authors(conn)
    if not authors:
        log.warning("no accepted author matches — nothing to refresh")
        return {"authors": 0}

    since = since or delta_since(conn, JOB, default_days=14)
    log.info("delta: works updated since %s for %s authors", since, f"{len(authors):,}")

    author_ids = sorted(authors)
    stats = {"authors": len(author_ids), "works": 0, "authorships": 0, "batches": 0}

    for start in range(0, len(author_ids), AUTHORS_PER_CALL):
        chunk = author_ids[start:start + AUTHORS_PER_CALL]
        work_rows: list[dict] = []
        authorship_rows: list[dict] = []

        try:
            records = list(_walk(http, conn, settings, chunk, since))
        except Exception as exc:
            # A failed batch leaves its cursor in place, so the next run resumes
            # from exactly where it stopped rather than re-walking from scratch.
            log.warning("delta batch failed for %d authors: %s", len(chunk), exc)
            continue

        for record in records:
            slim = slim_work(record)
            if not slim:
                continue
            work_rows.append(slim)
            for authorship in record.get("authorships") or []:
                author_id = openalex_id((authorship.get("author") or {}).get("id"))
                for npi, confidence in authors.get(author_id, ()):
                    authorship_rows.append({
                        "npi": npi,
                        "work_id": slim["work_id"],
                        "author_id": author_id,
                        "position": position_of(authorship),
                        "confidence": confidence,
                    })

        if work_rows:
            upsert(conn, UPSERT_WORK, work_rows, commit_each_batch=False)
            upsert(conn, UPSERT_AUTHORSHIP, authorship_rows, commit_each_batch=False)
            conn.commit()
        stats["works"] += len(work_rows)
        stats["authorships"] += len(authorship_rows)
        stats["batches"] += 1

        if stats["batches"] % 50 == 0:
            log.info("  … %d author batches, %s works", stats["batches"], f"{stats['works']:,}")

    mark_finished(conn, JOB)
    analyze(conn, "work", "authorship")
    log.info("delta complete: %s works, %s authorships", f"{stats['works']:,}",
             f"{stats['authorships']:,}")
    return stats


def search_fallback(conn: psycopg.Connection, settings: Settings, limit: int = 500) -> dict:
    """Per-physician author search for the still-unmatched. Refresh only.

    `search=` calls cost 10× a list call ($0.001 vs $0.0001), which is fine
    one-off and ruinous ×1M — so this is never a seeding path. It exists for the
    quarterly refresh, aimed at physicians the local pool never produced a
    candidate for at all.
    """
    http = client_for(settings, "openalex")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.npi, f.first_name, f.last_name, f.practice_state
            FROM staging.physician_match_facts f
            LEFT JOIN physician_author pa ON pa.npi = f.npi
            WHERE pa.npi IS NULL
            ORDER BY f.npi
            LIMIT %s
            """,
            (limit,),
        )
        unmatched = cur.fetchall()

    log.info("search fallback for %s unmatched physicians", f"{len(unmatched):,}")
    found = 0
    rows = []
    for npi, first, last, _state in unmatched:
        params = {
            "search": f"{first} {last}",
            "per-page": 25,
            "mailto": settings.contact_email,
        }
        if settings.openalex_api_key:
            params["api_key"] = settings.openalex_api_key
        try:
            data = http.get_json("https://api.openalex.org/authors", params=params)
        except Exception as exc:
            log.debug("search failed for %s: %s", npi, exc)
            continue
        for author in data.get("results", [])[:25]:
            author_id = openalex_id(author.get("id"))
            display = author.get("display_name") or ""
            if author_id:
                rows.append({
                    "openalex_author_id": author_id,
                    "display_name": display,
                    "orcid": (author.get("orcid") or "").rsplit("/", 1)[-1] or None,
                    "works_count": author.get("works_count") or 0,
                    # Without this the candidate is invisible to the matcher: the
                    # pool index is partial on `name_key IS NOT NULL` and the
                    # candidate join is on that column.
                    "name_key": name_key(display),
                })
        found += 1

    # Candidates land in the pool; scoring is then the same local matcher, so the
    # fallback never gets its own (divergent) definition of a good match.
    written = upsert(
        conn,
        """
        INSERT INTO staging.openalex_author_pool
            (openalex_author_id, display_name, orcid, works_count, name_key, loaded_at)
        VALUES (%(openalex_author_id)s, %(display_name)s, %(orcid)s, %(works_count)s,
                %(name_key)s, now())
        ON CONFLICT (openalex_author_id) DO NOTHING
        """,
        rows,
    )
    log.info("search fallback added %s pool candidates for %s physicians",
             f"{written:,}", f"{found:,}")
    return {"searched": found, "candidates_added": written}
