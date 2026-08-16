"""Load the NUCC taxonomy code list into the `taxonomy` reference table.

Small, static, and a hard prerequisite for everything else in bucket 1:
`physician_taxonomy.taxonomy_code` has a foreign key into this table, and the
"is this person a physician?" filter is a taxonomy-code test. Load this before
the physician upsert, not after.

NUCC publishes twice a year (January and July), so this refreshes on that cycle.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import psycopg

from ...db import analyze, upsert

log = logging.getLogger("pex.nppes.taxonomy")

UPSERT = """
INSERT INTO taxonomy (code, "grouping", classification, specialization,
                      display_name, is_physician)
VALUES (%(code)s, %(grouping)s, %(classification)s, %(specialization)s,
        %(display_name)s, %(is_physician)s)
ON CONFLICT (code) DO UPDATE SET
    "grouping"     = EXCLUDED."grouping",
    classification = EXCLUDED.classification,
    specialization = EXCLUDED.specialization,
    display_name   = EXCLUDED.display_name,
    is_physician   = EXCLUDED.is_physician
"""


def _get(row: dict, *names: str) -> str:
    """Fetch a column by any of its known header spellings."""
    for name in names:
        for key, value in row.items():
            if key and key.strip().lower() == name:
                return (value or "").strip()
    return ""


def parse(csv_path: Path) -> list[dict]:
    """Read the NUCC CSV into `taxonomy` row dicts.

    `display_name` is the patient-facing name we show. NUCC's own "Display Name"
    column is used when present; otherwise it is composed from the classification
    and specialization, because the column is NOT NULL and a blank specialty label
    in the UI is worse than a slightly clumsy generated one.

    `is_physician` is ours, not NUCC's: codes beginning `20` are the Allopathic &
    Osteopathic Physicians grouping — MDs and DOs — which is the v1 definition of
    "is this a doctor".
    """
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        for raw in csv.DictReader(fh):
            code = _get(raw, "code")
            if not code or len(code) != 10:
                continue
            grouping = _get(raw, "grouping")
            classification = _get(raw, "classification")
            specialization = _get(raw, "specialization")
            display = _get(raw, "display name", "displayname")
            if not display:
                display = " — ".join(p for p in (classification, specialization) if p) or code
            rows.append(
                {
                    "code": code,
                    "grouping": grouping or None,
                    "classification": classification or None,
                    "specialization": specialization or None,
                    "display_name": display,
                    "is_physician": code.startswith("20"),
                }
            )
    return rows


def load(conn: psycopg.Connection, csv_path: Path) -> int:
    rows = parse(csv_path)
    if len(rows) < 500:
        # The real file carries ~870 codes; a much shorter one means a truncated
        # download or a changed layout, and loading it would quietly shrink the
        # set of codes the physician filter can even recognize.
        raise RuntimeError(f"NUCC file parsed to only {len(rows)} codes — expected ~870")

    written = upsert(conn, UPSERT, rows)
    analyze(conn, "taxonomy")
    physicians = sum(1 for r in rows if r["is_physician"])
    log.info("taxonomy: %d codes loaded (%d flagged is_physician)", written, physicians)
    return written
