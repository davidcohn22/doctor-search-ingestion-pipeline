"""Fill ORCID records from ORCID's annual Public Data File instead of the API.

The public API allows 25,000 anonymous reads per day per IP (ORCID FAQ, "What are
the API limits?"); the first fetch hit that at 24,520 records with ~104,000 to
go. ORCID publishes every public record once a year under CC0, and the
"summaries" archive holds exactly what the check needs: employments, education,
qualifications and the works list with DOIs and PMIDs.

    pex run publications --orcid-dump ~/pex-data/orcid/ORCID_2025_10_summaries.tar.gz

One streaming pass over the archive (46 GB compressed). Member names carry the
ORCID ("ORCID_2025_10_summaries/000/0000-0001-5088-1000.xml"), so only the
records we want are parsed. Records already fetched live are left alone; dump
records are stored with source 'dump_2025_10'.
"""

from __future__ import annotations

import logging
import subprocess
import tarfile
from pathlib import Path

import psycopg
from lxml import etree

from ...db import Jsonb, upsert
from ...us_states import to_code
from .orcid_check import HELD_MIN_CONFIDENCE, TODO_ORCIDS

log = logging.getLogger("pex.orcid.dump")

AFFILIATION_KINDS = {"employment-summary": "employment", "education-summary": "education",
                     "qualification-summary": "qualification",
                     "invited-position-summary": "invited-position"}

UPSERT = """
INSERT INTO staging.orcid_record (orcid, http_status, given_names, family_name,
                                  affiliations, dois, pmids, fetched_at, source)
VALUES (%(orcid)s, 200, %(given)s, %(family)s, %(affiliations)s, %(dois)s, %(pmids)s,
        now(), %(source)s)
ON CONFLICT (orcid) DO NOTHING
"""


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(node, name):
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _text(node, *path) -> str | None:
    for name in path:
        if node is None:
            return None
        node = _child(node, name)
    return node.text.strip() if node is not None and node.text else None


def _year(node, field) -> int | None:
    value = _text(node, field, "year")
    return int(value) if value and value.isdigit() else None


def parse_summary(xml: bytes) -> dict:
    """The same shape as orcid_check.parse_record, from a summary XML document."""
    root = etree.fromstring(xml)
    affiliations, dois, pmids = [], set(), set()
    given = family = None
    for node in root.iter():
        name = _local(node.tag)
        if name == "name" and given is None and _text(node, "given-names"):
            given, family = _text(node, "given-names"), _text(node, "family-name")
        elif name in AFFILIATION_KINDS:
            organization = _child(node, "organization")
            country = (_text(organization, "address", "country") or "").upper() or None
            affiliations.append({
                "type": AFFILIATION_KINDS[name],
                "org": _text(organization, "name"),
                "country": country,
                "state": to_code(_text(organization, "address", "region"))
                if country == "US" else None,
                "start": _year(node, "start-date"),
                "end": _year(node, "end-date"),
            })
        elif name == "works":
            # Only the works list: fundings and peer reviews carry external ids
            # too, and those DOIs are not the person's own papers.
            for ext in node.iter():
                if _local(ext.tag) != "external-id":
                    continue
                kind = (_text(ext, "external-id-type") or "").lower()
                value = _text(ext, "external-id-normalized") or _text(ext, "external-id-value")
                if not value:
                    continue
                if kind == "doi":
                    dois.add(value.lower().removeprefix("https://doi.org/"))
                elif kind == "pmid":
                    pmids.add(value.rstrip("/").rsplit("/", 1)[-1])
    return {"given": given, "family": family, "affiliations": affiliations,
            "dois": sorted(dois), "pmids": sorted(pmids)}


def load(conn: psycopg.Connection, archive: Path, source: str = "dump_2025_10") -> dict:
    with conn.cursor() as cur:
        cur.execute(TODO_ORCIDS, {"held_min": HELD_MIN_CONFIDENCE})
        wanted = {row[0] for row in cur.fetchall()}
    log.info("looking for %s ORCIDs in %s", f"{len(wanted):,}", archive.name)

    stats = {"members": 0, "found": 0}
    batch: list[dict] = []
    # gzip in a separate process: decompression is the bottleneck, and this keeps
    # it off the parsing thread.
    proc = subprocess.Popen(["gzip", "-dc", str(archive)], stdout=subprocess.PIPE,
                            bufsize=1 << 20)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                stats["members"] += 1
                if stats["members"] % 2_000_000 == 0:
                    log.info("  … %s records scanned, %s found",
                             f"{stats['members']:,}", f"{stats['found']:,}")
                orcid = member.name.rsplit("/", 1)[-1].removesuffix(".xml")
                if orcid not in wanted:
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                parsed = parse_summary(handle.read())
                stats["found"] += 1
                batch.append({
                    "orcid": orcid, "given": parsed["given"], "family": parsed["family"],
                    "affiliations": Jsonb(parsed["affiliations"]),
                    "dois": Jsonb(parsed["dois"]), "pmids": Jsonb(parsed["pmids"]),
                    "source": source,
                })
                if len(batch) >= 2_000:
                    upsert(conn, UPSERT, batch)
                    batch.clear()
    finally:
        proc.stdout.close()
        proc.wait()
    if batch:
        upsert(conn, UPSERT, batch)
    stats["not_in_dump"] = len(wanted) - stats["found"]
    log.info("ORCID dump: %s", stats)
    return stats
