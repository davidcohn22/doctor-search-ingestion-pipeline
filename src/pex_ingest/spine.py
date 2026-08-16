"""The matcher's view of the physician spine.

Buckets 3 and 4 both need the same two things about every physician, and both
need them keyed for fast candidate lookup:

  * **`staging.physician_name_key`** — one row per (name_key, npi), including
    every NPPES alternate name. This is the candidate-retrieval index: a name key
    from an author record or a trial contact joins straight to the NPIs that
    could plausibly be that person.
  * **`staging.physician_match_facts`** — the scoring inputs (names, practice
    geography, specialty labels, credential) denormalized into one row per
    physician, so scoring 11M candidate pairs is not 11M joins across three
    tables.

Both are derived, disposable, and rebuilt after every spine load. They live in
`staging` because they are ingestion scaffolding: nothing the application reads
depends on them.

Alternate names matter more than they look. A surgeon who published under a
maiden name is invisible to a matcher that only indexes the current legal name,
and that is the single most common cause of a false negative — which is why
`name_keys_for_physician` folds `physician.other_names` in.
"""

from __future__ import annotations

import json
import logging

import psycopg

from .db import analyze, copy_rows, iter_rows, truncate
from .names import full_name, name_keys_for_physician
from .specialties import labels_for_taxonomies

log = logging.getLogger("pex.spine")

READ_PHYSICIANS = """
SELECT
    p.npi,
    p.first_name,
    p.middle_name,
    p.last_name,
    p.credential,
    p.practice_city,
    p.practice_state,
    p.other_names,
    coalesce(
        (SELECT jsonb_agg(DISTINCT t.display_name)
           FROM physician_taxonomy pt
           JOIN taxonomy t ON t.code = pt.taxonomy_code
          WHERE pt.npi = p.npi),
        '[]'::jsonb
    ) AS taxonomy_names
FROM physician p
WHERE p.nppes_status = 'active'
"""


def rebuild(conn: psycopg.Connection, settings) -> dict:
    """Rebuild both staging tables from the current spine.

    Truncate-and-rebuild rather than incremental: the whole thing is a few
    million rows derived from data we already hold, it takes minutes, and a
    rebuild can never leave a stale key pointing at a renamed physician.

    Reads over a *second* connection, because the read is a server-side cursor
    streaming a million rows while the write side is running COPY — interleaving
    those on one connection is asking for trouble.
    """
    from .db import connect

    truncate(conn, "staging.physician_name_key")
    truncate(conn, "staging.physician_match_facts")

    stats = {"physicians": 0, "name_keys": 0, "no_key": 0}
    facts_batch: list = []
    keys_batch: list = []

    with connect(settings) as reader:
        for row in iter_rows(reader, READ_PHYSICIANS, name="spine_read"):
            (npi, first, middle, last, credential, city, state, other_names,
             taxonomy_names) = row
            stats["physicians"] += 1

            other = other_names if isinstance(other_names, list) else []
            labels = labels_for_taxonomies(taxonomy_names or [])
            keys = name_keys_for_physician(first or "", last or "", other)
            if not keys:
                stats["no_key"] += 1

            facts_batch.append((
                npi, first, middle, last, credential, city, state,
                full_name(first or "", middle or "", last or ""),
                json.dumps(sorted(labels)),
                json.dumps(sorted(keys)),
            ))
            keys_batch.extend((key, npi) for key in keys)

            # Flush in batches so memory stays flat over ~1M physicians.
            if len(facts_batch) >= 50_000:
                _flush(conn, facts_batch, keys_batch, stats)
                facts_batch, keys_batch = [], []

    _flush(conn, facts_batch, keys_batch, stats)

    analyze(conn, "staging.physician_name_key", "staging.physician_match_facts")
    log.info(
        "spine index: %s physicians → %s name keys (%s had no usable key)",
        f"{stats['physicians']:,}", f"{stats['name_keys']:,}", f"{stats['no_key']:,}",
    )
    return stats


def _flush(conn, facts_batch, keys_batch, stats) -> None:
    if facts_batch:
        copy_rows(
            conn,
            "staging.physician_match_facts",
            ["npi", "first_name", "middle_name", "last_name", "credential",
             "practice_city", "practice_state", "full_name", "specialty_labels",
             "name_keys"],
            facts_batch,
        )
    if keys_batch:
        copy_rows(conn, "staging.physician_name_key", ["name_key", "npi"], keys_batch)
        stats["name_keys"] += len(keys_batch)


def namesake_counts(conn: psycopg.Connection) -> dict:
    """How many physicians share each name key.

    Name rarity is one of the strongest available signals and the one most often
    assumed rather than measured. "Hasni|s" matching one NPI in the whole country
    is near-proof; "smith|j" matching 1,100 is close to no evidence at all. This
    measures it in the registry itself — the same approach
    scripts/bootstrap_eval_set.py takes with its NPPES namesake count.
    """
    counts = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name_key, count(DISTINCT npi) FROM staging.physician_name_key "
            "GROUP BY name_key"
        )
        for key, count in cur.fetchall():
            counts[key] = count
    return counts
