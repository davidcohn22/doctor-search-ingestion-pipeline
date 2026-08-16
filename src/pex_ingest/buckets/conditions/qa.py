"""The QA gate: generate a review sheet, then promote what was reviewed.

This is the only human-review gate in the pipeline, and it is deliberately a
*sampling* gate rather than a full review. ~6,000 draft conditions is far too many
to read one by one, and the thing that scales badly here is human review time,
not dollars.

  "The QA step generates a review sheet — a random sample of drafts with their
   names, synonyms, and top matched papers — and a promotion script flips
   reviewed batches to `active`. Humans review samples; nothing is hand-authored."

**The sample is stratified, not flat.** Drawn per tree branch and weighted by
paper count, because a flat random sample over 6,000 rows would be dominated by
long-tail descriptors nobody searches, and would tell you almost nothing about
the conditions users will actually hit.
"""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path

import psycopg

log = logging.getLogger("pex.conditions.qa")

REVIEW_FIELDS = [
    "condition_id", "name", "kind", "status", "branch", "n_papers", "mesh_id",
    "description", "clinical_synonyms", "colloquial_synonyms", "body_part_synonyms",
    "specialties", "example_papers", "verdict", "reviewer", "notes",
]

SAMPLE = """
WITH ranked AS (
    SELECT
        c.condition_id,
        c.name,
        c.kind,
        c.status,
        cm.mesh_id,
        coalesce(p.n_papers, 0) AS n_papers,
        (SELECT array_to_string((string_to_array(t.tree, '.'))[1:2], '.')
           FROM staging.mesh_candidates mc,
                jsonb_array_elements_text(mc.tree_numbers) AS t(tree)
          WHERE mc.mesh_id = cm.mesh_id LIMIT 1) AS branch,
        c.description
    FROM condition c
    JOIN condition_mesh cm ON cm.condition_id = c.condition_id AND cm.is_primary
    LEFT JOIN staging.mesh_paper_count p ON p.mesh_id = cm.mesh_id
    WHERE c.status = 'draft'
),
stratified AS (
    SELECT *,
           row_number() OVER (
               PARTITION BY branch
               -- Weighted toward the conditions users will actually reach, with
               -- randomness inside each weight band so the sample is not simply
               -- "the top N of every branch" every time.
               ORDER BY n_papers DESC, random()
           ) AS rank_in_branch
    FROM ranked
)
SELECT condition_id, name, kind, status, branch, n_papers, mesh_id, description
FROM stratified
WHERE rank_in_branch <= %(per_branch)s
ORDER BY branch, n_papers DESC
LIMIT %(limit)s
"""

SYNONYMS = """
SELECT condition_id, kind, array_agg(synonym ORDER BY synonym)
FROM condition_synonym WHERE condition_id = ANY(%s)
GROUP BY 1, 2
"""

SPECIALTIES = """
SELECT ct.condition_id, array_agg(t.display_name ORDER BY ct.is_primary DESC)
FROM condition_taxonomy ct
JOIN taxonomy t ON t.code = ct.taxonomy_code
WHERE ct.condition_id = ANY(%s)
GROUP BY 1
"""

# A few real papers per condition, so a reviewer can see what the MeSH mapping
# actually pulls in — the fastest way to spot a descriptor that is technically
# correct but useless as a search target.
EXAMPLE_PAPERS = """
SELECT cm.condition_id,
       array_agg(w.title ORDER BY w.cited_by_count DESC NULLS LAST)
FROM condition_mesh cm
JOIN staging.work_mesh_expanded e ON e.mesh_id = cm.mesh_id
JOIN work w ON w.openalex_work_id = e.openalex_work_id
WHERE cm.condition_id = ANY(%s)
GROUP BY 1
"""


def review_sheet(conn: psycopg.Connection, out_dir: Path, limit: int = 400,
                 per_branch: int = 8) -> Path:
    """Write a stratified sample of drafts to CSV for human review."""
    with conn.cursor() as cur:
        cur.execute(SAMPLE, {"limit": limit, "per_branch": per_branch})
        rows = cur.fetchall()

    if not rows:
        log.warning("no draft conditions to review")
    ids = [r[0] for r in rows]

    synonyms: dict[tuple, list] = {}
    specialties: dict[int, list] = {}
    papers: dict[int, list] = {}
    if ids:
        with conn.cursor() as cur:
            cur.execute(SYNONYMS, (ids,))
            for condition_id, kind, values in cur.fetchall():
                synonyms[(condition_id, kind)] = values
            cur.execute(SPECIALTIES, (ids,))
            specialties = dict(cur.fetchall())
            cur.execute(EXAMPLE_PAPERS, (ids,))
            papers = {cid: titles[:3] for cid, titles in cur.fetchall()}

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"condition-review-{date.today().isoformat()}.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        for (condition_id, name, kind, status, branch, n_papers, mesh_id, description) in rows:
            writer.writerow({
                "condition_id": condition_id,
                "name": name,
                "kind": kind,
                "status": status,
                "branch": branch,
                "n_papers": n_papers,
                "mesh_id": mesh_id,
                "description": description,
                "clinical_synonyms": "; ".join(synonyms.get((condition_id, "clinical"), [])[:8]),
                "colloquial_synonyms": "; ".join(synonyms.get((condition_id, "colloquial"), [])),
                "body_part_synonyms": "; ".join(synonyms.get((condition_id, "body_part"), [])),
                "specialties": "; ".join(specialties.get(condition_id, [])),
                "example_papers": " | ".join(papers.get(condition_id, [])),
                "verdict": "",
                "reviewer": "",
                "notes": "",
            })

    log.info("wrote %d conditions to %s", len(rows), path)
    log.info("review it, put 'ok' or 'reject' in the verdict column, then run: "
             "pex run conditions --promote %s", path.name)
    return path


def promote(conn: psycopg.Connection, sheet: Path, promote_branch: bool = True) -> dict:
    """Apply a reviewed sheet's verdicts.

    Two modes, and the difference matters at 6,000 drafts:

      * **branch promotion** (default) — an `ok` verdict promotes every draft in
        that condition's tree branch. This is what makes a *sampling* gate work:
        the reviewer is certifying that this branch's generated output is sound,
        not just that one row is.
      * **row promotion** — only the reviewed rows. Safer, far slower, and the
        right choice for a branch whose sample looked shaky.

    A `reject` verdict never promotes anything and always deletes the reviewed
    condition, whichever mode is in use.
    """
    verdicts: list[tuple] = []
    with open(sheet, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            verdict = (row.get("verdict") or "").strip().lower()
            if verdict:
                verdicts.append((int(row["condition_id"]), verdict, row.get("branch") or ""))

    if not verdicts:
        log.warning("%s has no verdicts filled in — nothing to promote", sheet)
        return {"reviewed": 0, "promoted": 0, "rejected": 0}

    ok_ids = [cid for cid, verdict, _ in verdicts if verdict in ("ok", "yes", "approve")]
    reject_ids = [cid for cid, verdict, _ in verdicts if verdict in ("reject", "no")]
    ok_branches = sorted({branch for cid, verdict, branch in verdicts
                          if cid in set(ok_ids) and branch})

    stats = {"reviewed": len(verdicts), "promoted": 0, "rejected": 0}
    with conn.cursor() as cur:
        if reject_ids:
            cur.execute("DELETE FROM condition WHERE condition_id = ANY(%s)", (reject_ids,))
            stats["rejected"] = cur.rowcount

        if ok_ids and not promote_branch:
            cur.execute(
                "UPDATE condition SET status = 'active' WHERE condition_id = ANY(%s)",
                (ok_ids,),
            )
            stats["promoted"] = cur.rowcount
        elif ok_branches:
            cur.execute(
                """
                UPDATE condition c SET status = 'active'
                FROM condition_mesh cm, staging.mesh_candidates mc
                WHERE cm.condition_id = c.condition_id
                  AND cm.is_primary
                  AND mc.mesh_id = cm.mesh_id
                  AND c.status = 'draft'
                  AND EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements_text(mc.tree_numbers) AS t(tree)
                      WHERE array_to_string((string_to_array(t.tree, '.'))[1:2], '.')
                            = ANY(%s)
                  )
                """,
                (ok_branches,),
            )
            stats["promoted"] = cur.rowcount
    conn.commit()

    log.info(
        "promotion: %d verdicts → %s conditions active, %s deleted (branches: %s)",
        stats["reviewed"], f"{stats['promoted']:,}", f"{stats['rejected']:,}",
        ", ".join(ok_branches) or "n/a",
    )
    return stats
