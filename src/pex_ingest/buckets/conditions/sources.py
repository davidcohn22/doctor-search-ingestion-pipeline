"""Resolve and download the two NLM files bucket 5 needs.

Both are free, public and unauthenticated:

  * **MeSH descriptors** — `desc<YEAR>.gz`, 16 MB gzipped rather than the 298 MB
    raw XML. It is 18× smaller and `lxml` streams straight out of it.
  * **MedlinePlus health topics** — ~1,000 consumer-health topics, each carrying
    its MeSH heading. A ready-made "patient-friendly name for this descriptor"
    crosswalk, which is why the LLM pass only has to fill gaps rather than name
    everything.

Neither URL is stable across years. The MeSH release lands each December, so the
year is resolved by trying the next one and falling back — hardcoding `2026`
guarantees a silent staleness the following January. MedlinePlus files are dated
in the filename, so the current one is read off the landing page.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from ...config import Settings

log = logging.getLogger("pex.conditions.sources")

MESH_BASE = "https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/"
MEDLINEPLUS_INDEX = "https://medlineplus.gov/xml.html"
MEDLINEPLUS_BASE = "https://medlineplus.gov/xml/"


def resolve_mesh_year(http, latest: int | None = None) -> int:
    """The newest published MeSH year.

    `desc2027.*` returns a 302 until it is published, so this walks back from next
    year to the first that resolves.
    """
    start = latest or (date.today().year + 1)
    for year in range(start, start - 4, -1):
        url = f"{MESH_BASE}desc{year}.gz"
        size = http.remote_size(url)
        if size and size > 1_000_000:
            log.info("MeSH release %d available (%.1f MB)", year, size / 1e6)
            return year
    raise RuntimeError(f"No MeSH descriptor file found under {MESH_BASE}")


def download_mesh(http, settings: Settings, year: int | None = None) -> tuple[Path, int]:
    year = year or resolve_mesh_year(http)
    url = f"{MESH_BASE}desc{year}.gz"
    path = http.download(url, settings.snapshot_dir("mesh", str(year)) / f"desc{year}.gz")
    return path, year


def resolve_medlineplus(http) -> str:
    """The current dated topics file, e.g. mplus_topics_2026-08-14.xml."""
    html = http.get_text(MEDLINEPLUS_INDEX)
    names = re.findall(r"(mplus_topics_\d{4}-\d{2}-\d{2}\.xml)", html)
    if not names:
        raise RuntimeError(f"No mplus_topics_*.xml link found on {MEDLINEPLUS_INDEX}")
    newest = max(names)  # ISO dates sort correctly as strings
    log.info("MedlinePlus topics file: %s", newest)
    return MEDLINEPLUS_BASE + newest


def download_medlineplus(http, settings: Settings, year: int) -> Path:
    url = resolve_medlineplus(http)
    return http.download(url, settings.snapshot_dir("mesh", str(year)) / url.rsplit("/", 1)[-1])
