"""Resolving the current NPPES and NUCC download URLs.

Government file names embed dates and change every month and every week, and
there is no stable "latest" URL — so the current link has to be read off the
landing page on every run rather than hardcoded.

Two automation gotchas, both from docs/prerequisites/01-nppes-identity.md and
both discovered by trying it:

  * the landing page returns **HTTP 403** without a browser User-Agent (handled
    by `http.client_for("nppes")`, which sets one),
  * the links are **relative** (`href="./NPPES_…"`), so they must be joined to
    the base URL.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

log = logging.getLogger("pex.nppes.sources")

NPPES_BASE = "https://download.cms.gov/nppes/"
NPPES_INDEX = NPPES_BASE + "NPI_Files.html"

NUCC_INDEX = (
    "https://www.nucc.org/index.php/code-sets-mainmenu-41/"
    "provider-taxonomy-mainmenu-40/csv-mainmenu-57"
)
NUCC_BASE = "https://www.nucc.org/images/stories/CSV/"

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


@dataclass(frozen=True)
class NppesFile:
    """One resolvable NPPES download, with the snapshot directory it belongs in."""

    kind: str          # 'monthly' | 'weekly' | 'deactivated'
    url: str
    filename: str
    release: str       # snapshot directory name, e.g. '2026-08-full'


def _release_from_monthly(filename: str) -> str:
    """'NPPES_Data_Dissemination_August_2026_V2.zip' → '2026-08-full'."""
    match = re.search(r"_([A-Za-z]+)_(\d{4})", filename)
    if match and match.group(1).lower() in MONTHS:
        return f"{match.group(2)}-{MONTHS[match.group(1).lower()]:02d}-full"
    return date.today().strftime("%Y-%m-full")


def _release_from_weekly(filename: str) -> str:
    """'…_080326_080926_Weekly_V2.zip' → '2026-08-03_2026-08-09'.

    The dates are MMDDYY pairs. Keeping the real week in the directory name is
    what makes a missed week visible on the filesystem rather than something you
    have to reconstruct from the database.
    """
    match = re.search(r"_(\d{6})_(\d{6})_Weekly", filename)
    if not match:
        return date.today().strftime("%Y-%m-%d-weekly")

    def parse(stamp: str) -> str:
        month, day, year = int(stamp[0:2]), int(stamp[2:4]), int(stamp[4:6])
        return f"20{year:02d}-{month:02d}-{day:02d}"

    return f"{parse(match.group(1))}_{parse(match.group(2))}"


def discover_nppes(http) -> dict[str, NppesFile]:
    """Read the landing page and return the current monthly/weekly/deactivated files."""
    html = http.get_text(NPPES_INDEX)
    # The page has used both quote styles (single quotes as of 2026-10).
    zips = re.findall(r"""href=["']\.?/?([^"']+\.zip)["']""", html, flags=re.IGNORECASE)
    if not zips:
        raise RuntimeError(
            f"No .zip links found on {NPPES_INDEX}. The page layout changed, or the "
            "request was rejected — check that a browser User-Agent is being sent."
        )

    found: dict[str, NppesFile] = {}
    for name in zips:
        base = name.rsplit("/", 1)[-1]
        lower = base.lower()
        if "weekly" in lower:
            kind, release = "weekly", _release_from_weekly(base)
        elif "deactivated" in lower:
            kind, release = "deactivated", date.today().strftime("%Y-%m")
        elif "data_dissemination" in lower:
            kind, release = "monthly", _release_from_monthly(base)
        else:
            continue
        # The page lists historical files too; keep the newest of each kind,
        # which sorts correctly because the release strings are ISO-ish.
        if kind not in found or release > found[kind].release:
            found[kind] = NppesFile(kind, NPPES_BASE + base, base, release)

    for kind, entry in sorted(found.items()):
        log.info("NPPES %-12s %s (release %s)", kind, entry.filename, entry.release)
    if "monthly" not in found:
        raise RuntimeError("Could not resolve the NPPES monthly full file from the landing page.")
    return found


def discover_nucc(http) -> tuple[str, str]:
    """Current NUCC taxonomy CSV as (url, version).

    Versions are `<year><release>` — '261' is 2026 release 1 — and NUCC keeps
    every historical file online, so the newest is the numerically highest, not
    the first or last in document order.
    """
    html = http.get_text(NUCC_INDEX)
    versions = re.findall(r"nucc_taxonomy_(\d+)\.csv", html)
    if not versions:
        raise RuntimeError(f"No taxonomy CSV links found on {NUCC_INDEX}")
    latest = max(versions, key=int)
    log.info("NUCC taxonomy version %s", latest)
    return f"{NUCC_BASE}nucc_taxonomy_{latest}.csv", latest


def main_csv_in(extract_dir) -> "Path":  # noqa: F821 - Path imported by caller
    """Find `npidata_pfile_<from>-<to>.csv` inside an extracted NPPES ZIP.

    The ZIP also contains `pl_pfile_*` (secondary practice addresses, not
    ingested at v1), `othername_pfile_*` (already represented in the main file's
    Other Provider Name columns) and `endpoint_pfile_*` (unused). The `_fileheader`
    variants are one-line header files and must not be mistaken for the data.
    """
    from pathlib import Path

    candidates = [
        p for p in Path(extract_dir).glob("npidata_pfile_*.csv")
        if "fileheader" not in p.name.lower()
    ]
    if not candidates:
        raise FileNotFoundError(f"No npidata_pfile_*.csv found in {extract_dir}")
    # If several exist, the largest is the data file.
    return max(candidates, key=lambda p: p.stat().st_size)
