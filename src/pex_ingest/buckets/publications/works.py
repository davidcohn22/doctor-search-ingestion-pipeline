"""Pull works from the snapshot for matched authors, and keep the edge list.

One streaming pass over the projected works snapshot (~90 GB of 725 GB) does two
jobs at once:

  1. **Write `work` + `authorship` rows** for authors already matched to a
     physician — the product data.
  2. **Write a slim `(author_id, work_id, author_position)` edge list for *every*
     US-affiliated author**, matched or not.

Job 2 is the part that turns the snapshot from a one-time shortcut into the
durable design (decision 0001, "The extra win"). When the matcher improves and
newly matches 20,000 physicians, their works are **already local** — no new
download, no API spend. Without it, every matcher improvement re-opens the
question of how to fetch works for the newly matched, which is the exact problem
the snapshot was adopted to solve.

**Author position is scoring input, not decoration.** In medicine the first
author did the work and the last ("senior") author led it; middle authors count
for far less. `physician_condition_features.first_last_pub_count` depends on
getting this right.
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

import psycopg

from ...config import Settings
from ...db import Jsonb, analyze, copy_rows, upsert
from ...state import add_marker, get_marker
from .snapshot import WORK_COLUMNS, manifest, openalex_id, read_part, save_manifest

log = logging.getLogger("pex.openalex.works")

JOB = "openalex_works"

UPSERT_WORK = """
INSERT INTO work (openalex_work_id, doi, pmid, title, publication_year, journal_name,
                  openalex_source_id, work_type, topic_ids, mesh_terms, cited_by_count,
                  raw, retrieved_at)
VALUES (%(work_id)s, %(doi)s, %(pmid)s, %(title)s, %(year)s, %(journal)s,
        %(source_id)s, %(type)s, %(topic_ids)s, %(mesh_terms)s, %(cited_by)s,
        %(raw)s, now())
ON CONFLICT (openalex_work_id) DO UPDATE SET
    doi                = EXCLUDED.doi,
    pmid               = EXCLUDED.pmid,
    title              = EXCLUDED.title,
    publication_year   = EXCLUDED.publication_year,
    journal_name       = EXCLUDED.journal_name,
    openalex_source_id = EXCLUDED.openalex_source_id,
    work_type          = EXCLUDED.work_type,
    topic_ids          = EXCLUDED.topic_ids,
    -- Never overwrite MeSH we already hold with an empty array: the snapshot's
    -- `mesh` column is empty for recently published papers, and a later PubMed
    -- backfill is what fills them. Clobbering that on a quarterly re-sync would
    -- silently undo the enrichment.
    mesh_terms         = CASE
                            WHEN jsonb_array_length(EXCLUDED.mesh_terms) > 0
                            THEN EXCLUDED.mesh_terms
                            ELSE work.mesh_terms
                         END,
    cited_by_count     = EXCLUDED.cited_by_count,
    raw                = EXCLUDED.raw,
    retrieved_at       = now()
"""

UPSERT_AUTHORSHIP = """
INSERT INTO authorship (npi, openalex_work_id, openalex_author_id, author_position,
                        confidence, retrieved_at)
VALUES (%(npi)s, %(work_id)s, %(author_id)s, %(position)s, %(confidence)s, now())
ON CONFLICT (npi, openalex_work_id) DO UPDATE SET
    openalex_author_id = EXCLUDED.openalex_author_id,
    author_position    = EXCLUDED.author_position,
    confidence         = EXCLUDED.confidence,
    retrieved_at       = now()
"""

EDGE_COLUMNS = ["openalex_author_id", "openalex_work_id", "author_position"]


def matched_authors(conn: psycopg.Connection) -> dict[str, list[tuple[str, float]]]:
    """{author_id: [(npi, confidence), …]} for accepted matches only.

    Held and rejected matches are excluded deliberately: a held match is by
    definition one we are not confident about, and attaching its papers to a
    profile would defeat the purpose of holding it.
    """
    mapping: dict[str, list[tuple[str, float]]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT openalex_author_id, npi, confidence FROM physician_author "
            "WHERE status = 'accepted'"
        )
        for author_id, npi, confidence in cur.fetchall():
            mapping[author_id].append((npi, float(confidence)))
    log.info("%s accepted author identities across %s links",
             f"{len(mapping):,}", f"{sum(len(v) for v in mapping.values()):,}")
    return mapping


def position_of(authorship: dict) -> str:
    """Normalize OpenAlex's author_position to our CHECK-constrained vocabulary."""
    raw = (authorship.get("author_position") or "middle").lower()
    return raw if raw in ("first", "middle", "last") else "middle"


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


def slim_work(record: dict) -> dict | None:
    """Project one snapshot work record into a `work` row."""
    work_id = openalex_id(record.get("id"))
    if not work_id:
        return None

    ids = record.get("ids") or {}
    if not isinstance(ids, dict):
        # A parquet map column reads back as [(key, value), …].
        ids = dict(ids)
    pmid = ids.get("pmid")
    if pmid:
        # Arrives as a URL: https://pubmed.ncbi.nlm.nih.gov/12345678
        pmid = str(pmid).rstrip("/").rsplit("/", 1)[-1] or None

    location = record.get("primary_location") or {}
    source = location.get("source") or {}

    # "The payload contains duplicate descriptor entries (the same descriptor_ui
    # repeated within one work), so dedupe on load." — decision 0001
    mesh_terms = sorted({
        m.get("descriptor_ui") for m in (record.get("mesh") or [])
        if m and m.get("descriptor_ui")
    })

    doi = record.get("doi")
    return {
        "work_id": work_id,
        "doi": doi.rsplit("doi.org/", 1)[-1] if doi else None,
        "pmid": pmid,
        "title": (record.get("title") or "").strip() or "(untitled)",
        "year": record.get("publication_year"),
        "journal": source.get("display_name"),
        "source_id": openalex_id(source.get("id")),
        "type": record.get("type"),
        "topic_ids": Jsonb([openalex_id(t.get("id")) for t in (record.get("topics") or [])[:5]]),
        "mesh_terms": Jsonb(mesh_terms),
        "cited_by": record.get("cited_by_count"),
        # Parquet date → datetime.date, which JSON can't hold.
        "raw": Jsonb({"publication_date": _iso(record.get("publication_date"))}),
    }


def read_and_project(http, part, authors: dict, us_authors: set, keep_edges: bool):
    """Read one part file → (work rows, authorship rows, edge rows, records read)."""
    records = 0
    work_rows: list[dict] = []
    authorship_rows: list[dict] = []
    edge_rows: list[tuple] = []

    for record in read_part(http, part, WORK_COLUMNS):
        records += 1
        authorships = record.get("authorships") or []
        work_id = openalex_id(record.get("id"))
        if not work_id:
            continue

        relevant = False
        for authorship in authorships:
            author_id = openalex_id((authorship.get("author") or {}).get("id"))
            if not author_id:
                continue
            position = position_of(authorship)

            if keep_edges and author_id in us_authors:
                edge_rows.append((author_id, work_id, position))

            for npi, confidence in authors.get(author_id, ()):
                relevant = True
                authorship_rows.append({
                    "npi": npi,
                    "work_id": work_id,
                    "author_id": author_id,
                    "position": position,
                    # "A paper is only as certain as the identity match behind
                    # it" — inherited, never invented here.
                    "confidence": confidence,
                })

        if relevant:
            slim = slim_work(record)
            if slim:
                work_rows.append(slim)
    return work_rows, authorship_rows, edge_rows, records


def build(
    conn: psycopg.Connection,
    settings: Settings,
    http,
    keep_edges: bool = True,
    resume: bool = True,
    http_factory=None,
    workers: int = 1,
) -> dict:
    """Stream the works snapshot once; write product rows and the edge list."""
    http_factory = http_factory or (lambda: http)
    authors = matched_authors(conn)
    if not authors:
        raise RuntimeError(
            "No accepted physician↔author matches — run the matcher first, or there "
            "is nothing to pull works for."
        )
    us_authors = _pool_author_ids(conn) if keep_edges else set()

    parts = manifest(http, "works")
    save_manifest(parts, settings.path("openalex", "works", "manifest.json"))
    done = get_marker(conn, JOB) if resume else set()
    if done:
        log.info("resuming: %d of %d work part files already processed", len(done), len(parts))

    if keep_edges:
        drop_edge_indexes(conn)

    stats: defaultdict = defaultdict(int)
    todo = [(index, part) for index, part in enumerate(parts, start=1) if part.name not in done]

    # Reading a part is mostly waiting on S3 range requests, so a few parts are
    # read concurrently; writes and progress markers stay on this thread, in
    # manifest order, so resume semantics are unchanged. Only parts in flight
    # are held in memory, and each holds kept rows, never raw records.
    local = threading.local()

    def process(part):
        if not hasattr(local, "http"):
            local.http = http_factory()
        return read_and_project(local.http, part, authors, us_authors, keep_edges)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pending: deque = deque()
        queue = iter(todo)
        for item in itertools.islice(queue, max(1, workers)):
            pending.append((item, pool.submit(process, item[1])))
        while pending:
            (index, part), future = pending.popleft()
            work_rows, authorship_rows, edge_rows, records = future.result()
            nxt = next(queue, None)
            if nxt is not None:
                pending.append((nxt, pool.submit(process, nxt[1])))
            stats["records"] += records
            stats["works_kept"] += len(work_rows)

            if work_rows:
                # Works before authorships, always: `authorship` has a foreign key
                # into `work`, and a composite one into `physician_author`.
                upsert(conn, UPSERT_WORK, work_rows, commit_each_batch=False)
                upsert(conn, UPSERT_AUTHORSHIP, authorship_rows, commit_each_batch=False)
                conn.commit()
                stats["authorships"] += len(authorship_rows)
            if edge_rows:
                # OpenAlex occasionally lists one author twice on a work: keep the first.
                unique = list({(a, w): (a, w, pos) for a, w, pos in reversed(edge_rows)}.values())
                stats["edges"] += copy_rows(
                    conn, "staging.openalex_authorship_edge", EDGE_COLUMNS, unique
                )

            add_marker(conn, JOB, part.name)
            stats["parts"] += 1
            if index % 25 == 0:
                log.info(
                    "  … %d/%d parts | %s works kept, %s authorships, %s edges",
                    index, len(parts), f"{stats['works_kept']:,}",
                    f"{stats['authorships']:,}", f"{stats['edges']:,}",
                )

    if keep_edges:
        stats["edge_duplicates_removed"] = restore_edge_indexes(conn)
    analyze(conn, "work", "authorship")
    log.info(
        "works pass complete: %s records read, %s works kept, %s authorships, %s edges",
        f"{stats['records']:,}", f"{stats['works_kept']:,}",
        f"{stats['authorships']:,}", f"{stats['edges']:,}",
    )
    return dict(stats)


# The edge list reaches ~150M rows. Appending that through two B-trees means a
# random index read per row once they outgrow memory, which made the pass
# DB-bound at ~7 h. Instead the pass appends to a bare heap and builds both
# indexes once at the end, sorted. If a pass dies midway the indexes stay
# dropped until the resumed pass finishes; nothing reads the edges until then.
EDGE_PKEY = "openalex_authorship_edge_pkey"
EDGE_WORK_INDEX = "openalex_authorship_edge_work"


def drop_edge_indexes(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE staging.openalex_authorship_edge DROP CONSTRAINT IF EXISTS {EDGE_PKEY}")
        cur.execute(f"DROP INDEX IF EXISTS staging.{EDGE_WORK_INDEX}")
    conn.commit()
    log.info("edge-list indexes dropped for the bulk append; rebuilt at the end")


def restore_edge_indexes(conn: psycopg.Connection) -> int:
    """Remove duplicate edges (a part re-read after a crash), then rebuild indexes."""
    with conn.cursor() as cur:
        cur.execute("SET maintenance_work_mem = '2GB'")
        cur.execute("""
            DELETE FROM staging.openalex_authorship_edge e
            USING (
                SELECT ctid FROM (
                    SELECT ctid, row_number() OVER (
                        PARTITION BY openalex_author_id, openalex_work_id ORDER BY ctid) AS rn
                    FROM staging.openalex_authorship_edge
                ) ranked WHERE rn > 1
            ) dup
            WHERE e.ctid = dup.ctid
        """)
        removed = cur.rowcount
        log.info("edge list: %s duplicate rows removed; building indexes", f"{removed:,}")
        cur.execute(
            f"ALTER TABLE staging.openalex_authorship_edge "
            f"ADD CONSTRAINT {EDGE_PKEY} PRIMARY KEY (openalex_author_id, openalex_work_id)"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {EDGE_WORK_INDEX} "
            f"ON staging.openalex_authorship_edge (openalex_work_id)"
        )
        cur.execute("RESET maintenance_work_mem")
    conn.commit()
    analyze(conn, "staging.openalex_authorship_edge")
    return removed


def _pool_author_ids(conn: psycopg.Connection) -> set[str]:
    """Every author id in the pool — the edge list's scope.

    ~11M strings, a few hundred MB in memory. Held rather than queried per work
    because the alternative is a lookup per authorship across 510M works.
    """
    ids = set()
    with conn.cursor(name="pool_ids") as cur:
        cur.itersize = 100_000
        cur.execute("SELECT openalex_author_id FROM staging.openalex_author_pool")
        for (author_id,) in cur:
            ids.add(author_id)
    log.info("edge-list scope: %s pooled authors", f"{len(ids):,}")
    return ids


def backfill_from_edges(conn: psycopg.Connection) -> int:
    """Attach already-local works to authors matched *after* the works pass.

    This is the payoff of the edge list: a matcher improvement that newly accepts
    20,000 physicians costs one SQL statement instead of another 90 GB download.

    It only fills `authorship`. Any `work` row not already present cannot be
    conjured from the edge list, which carries no metadata — so the rare newly
    matched author whose papers were never kept still needs the API delta path.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO authorship (npi, openalex_work_id, openalex_author_id,
                                    author_position, confidence, retrieved_at)
            SELECT pa.npi, e.openalex_work_id, e.openalex_author_id,
                   e.author_position, pa.confidence, now()
            FROM staging.openalex_authorship_edge e
            JOIN physician_author pa ON pa.openalex_author_id = e.openalex_author_id
            JOIN work w ON w.openalex_work_id = e.openalex_work_id
            WHERE pa.status = 'accepted'
            ON CONFLICT (npi, openalex_work_id) DO NOTHING
            """
        )
        written = cur.rowcount
    conn.commit()
    log.info("backfilled %s authorship rows from the local edge list", f"{written:,}")
    return written
