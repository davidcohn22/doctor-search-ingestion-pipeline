"""MeSH enrichment from PubMed — the condition-relevance signal.

MeSH descriptors are what make "papers relevant to this condition" computable, so
this is the step everything downstream ranks on. It has three parts, and the
reason for the third is the one most likely to be skipped:

  | step | covers | cost |
  |------|--------|------|
  | 1. take `mesh` from the snapshot | ~80% of PMID works | $0, no calls |
  | 2. batched `efetch` backfill | most of the remaining ~20% | minutes |
  | 3. periodic re-check sweep | newly indexed recent papers | minutes/month |

Step 1 happens in `works.py`. This module is steps 2 and 3.

**Why step 3 exists.** NLM indexers assign MeSH *months after publication*, so
coverage climbs as papers age — ~44% for this year's papers, ~71% for 2020,
~78% for 2015. An empty `mesh` array on a brand-new paper does not mean OpenAlex
is missing data; it means nobody has MeSH for it yet, PubMed included. So a work
ingested at publication will never get MeSH unless something looks again:

  "Without this, your newest papers — often the most relevant ones for judging
   current expertise — stay permanently unclassified, and the failure is silent."

**Never one call per work.** `efetch` takes ~200 PMIDs per request and a free key
buys 10 requests/sec, which makes even the national gap (~1.5M works) about 13
minutes of traffic.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import psycopg

from ...config import Settings
from ...db import Jsonb, analyze, upsert
from ...http import client_for

log = logging.getLogger("pex.pubmed.mesh")

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
BATCH = 200  # efetch's practical per-request PMID ceiling

# Only re-check papers young enough that indexing is plausibly still catching up.
RECHECK_MAX_AGE_YEARS = 2

# Works with a PMID and no MeSH, never checked (or checked long enough ago to be
# worth another look).
NEEDS_MESH = """
SELECT openalex_work_id, pmid
FROM work
WHERE pmid IS NOT NULL
  AND jsonb_array_length(mesh_terms) = 0
  AND mesh_checked_at IS NULL
ORDER BY publication_year DESC NULLS LAST
"""

NEEDS_RECHECK = """
SELECT openalex_work_id, pmid
FROM work
WHERE pmid IS NOT NULL
  AND jsonb_array_length(mesh_terms) = 0
  AND mesh_checked_at IS NOT NULL
  AND mesh_checked_at < now() - interval '25 days'
  AND publication_year >= %s
ORDER BY publication_year DESC NULLS LAST
"""

UPDATE_MESH = """
UPDATE work
SET mesh_terms = CASE
                    WHEN jsonb_array_length(%(mesh_terms)s::jsonb) > 0
                    THEN %(mesh_terms)s::jsonb
                    ELSE mesh_terms
                 END,
    mesh_checked_at = now()
WHERE pmid = %(pmid)s
"""


def fetch_mesh(http, pmids: list[str], api_key: str, email: str) -> dict[str, list[str]]:
    """{pmid: [descriptor_ui, …]} for a batch of up to ~200 PMIDs.

    Returns an entry for every PMID that came back with an article record, even
    when its MeSH list is empty — that is a real answer ("PubMed has no MeSH for
    this yet") and is what lets `mesh_checked_at` be stamped honestly.
    """
    from lxml import etree

    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "tool": "physician-expertise-explorer",
        "email": email,
    }
    if api_key:
        params["api_key"] = api_key

    response = http.get(EFETCH, params=params)
    root = etree.fromstring(response.content)

    results: dict[str, list[str]] = {}
    for article in root.findall(".//PubmedArticle"):
        pmid_node = article.find(".//MedlineCitation/PMID")
        if pmid_node is None or not pmid_node.text:
            continue
        descriptors = []
        for heading in article.findall(".//MeshHeading/DescriptorName"):
            ui = heading.get("UI")
            if ui:
                descriptors.append(ui)
        # Dedupe: the same descriptor can legitimately repeat within one record.
        results[pmid_node.text.strip()] = sorted(set(descriptors))
    return results


def _pending(conn: psycopg.Connection, recheck: bool, limit: int | None) -> list[tuple]:
    from datetime import date

    with conn.cursor() as cur:
        if recheck:
            cur.execute(NEEDS_RECHECK, (date.today().year - RECHECK_MAX_AGE_YEARS,))
        else:
            cur.execute(NEEDS_MESH)
        rows = cur.fetchall()
    return rows[:limit] if limit else rows


def run(
    conn: psycopg.Connection,
    settings: Settings,
    recheck: bool = False,
    limit: int | None = None,
) -> dict:
    """Backfill (or re-check) MeSH terms for PMID-carrying works."""
    if not settings.ncbi_api_key:
        # Not fatal — E-utilities works unauthenticated — but three times slower,
        # and NCBI treats anonymous bulk traffic as abuse. The client already
        # paces itself down to 3 req/sec in this case.
        log.warning("NCBI_API_KEY is unset: pacing at 3 req/sec instead of 10")
    http = client_for(settings, "pubmed")

    pending = _pending(conn, recheck, limit)
    log.info(
        "%s works need a %s MeSH lookup (%d efetch calls at %d PMIDs each)",
        f"{len(pending):,}", "re-check" if recheck else "backfill",
        (len(pending) + BATCH - 1) // BATCH, BATCH,
    )
    stats = {"considered": len(pending), "filled": 0, "still_empty": 0, "calls": 0}
    if not pending:
        return stats

    def updates() -> Iterator[dict]:
        pmids = [p for _work_id, p in pending]
        for start in range(0, len(pmids), BATCH):
            chunk = pmids[start:start + BATCH]
            try:
                found = fetch_mesh(http, chunk, settings.ncbi_api_key, settings.ncbi_email)
            except Exception as exc:
                # One bad batch should not abandon the sweep; those PMIDs keep a
                # null mesh_checked_at and are simply picked up next run.
                log.warning("efetch batch failed (%d PMIDs): %s", len(chunk), exc)
                continue
            stats["calls"] += 1

            for pmid in chunk:
                descriptors = found.get(pmid)
                if descriptors is None:
                    continue  # not returned at all; leave unstamped for a retry
                if descriptors:
                    stats["filled"] += 1
                else:
                    stats["still_empty"] += 1
                yield {"pmid": pmid, "mesh_terms": Jsonb(descriptors)}

            if stats["calls"] % 100 == 0:
                log.info("  … %d calls, %s works filled", stats["calls"], f"{stats['filled']:,}")

    upsert(conn, UPDATE_MESH, updates(), batch_size=1_000)
    analyze(conn, "work")

    log.info(
        "MeSH %s: %s works filled, %s confirmed still unindexed by NLM",
        "re-check" if recheck else "backfill",
        f"{stats['filled']:,}", f"{stats['still_empty']:,}",
    )
    return stats


def coverage(conn: psycopg.Connection) -> dict:
    """MeSH coverage by publication year — the shape that proves it's working.

    A low figure for the current year is **expected and correct**: NLM has not
    indexed those papers yet. A low figure for papers five years old means the
    batching or the PMID join is broken. Reporting it by year is what makes those
    two cases distinguishable at a glance.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT publication_year,
                   count(*) FILTER (WHERE pmid IS NOT NULL) AS with_pmid,
                   count(*) FILTER (WHERE pmid IS NOT NULL
                                    AND jsonb_array_length(mesh_terms) > 0) AS with_mesh
            FROM work
            WHERE publication_year >= date_part('year', now()) - 12
            GROUP BY 1 ORDER BY 1 DESC
            """
        )
        rows = cur.fetchall()

    by_year = {}
    for year, with_pmid, with_mesh in rows:
        pct = round(100.0 * (with_mesh or 0) / max(with_pmid or 1, 1), 1)
        by_year[int(year)] = {"with_pmid": with_pmid, "with_mesh": with_mesh, "pct": pct}
        log.info("  %s: %s of %s PMID works have MeSH (%.1f%%)",
                 int(year), f"{with_mesh:,}", f"{with_pmid:,}", pct)
    return by_year
