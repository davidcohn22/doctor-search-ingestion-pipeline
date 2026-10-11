"""Write the four condition tables. Everything lands as `status = 'draft'`.

Name precedence, best source first:

  1. **MedlinePlus** — human-authored consumer health naming, where it exists,
  2. **the LLM pass** — for the majority MedlinePlus doesn't cover,
  3. **the MeSH descriptor name** — the always-available fallback, so a
     descriptor can never end up nameless.

Synonyms come from three places with different `kind` labels, because the search
box treats them differently: MeSH entry terms are `clinical` (or `abbreviation`),
and the LLM supplies `colloquial` and `body_part` — the phrasings that make
"elbow doctor" and "prostate problems" work at all.

**Nothing is promoted here.** This bucket's output is the only generated content
a user reads as product copy, so it stays `draft` until a human samples it. "A bad
synonym doesn't cause a crash — it causes a search that returns nothing, or worse,
the wrong specialty."
"""

from __future__ import annotations

import logging
import re

import psycopg

from ...db import analyze, upsert

log = logging.getLogger("pex.conditions.load")

# "A term of ≤5 uppercase letters is an abbreviation — crude but effective."
_ABBREVIATION = re.compile(r"^[A-Z0-9]{2,5}$")

UPSERT_CONDITION = """
INSERT INTO condition (name, kind, description, status)
VALUES (%(name)s, %(kind)s, %(description)s, 'draft')
ON CONFLICT (name) DO UPDATE SET
    kind        = EXCLUDED.kind,
    description = coalesce(EXCLUDED.description, condition.description)
    -- `status` is deliberately not touched: a condition already promoted to
    -- 'active' by review must not silently revert to draft on a re-run.
RETURNING condition_id
"""

# A condition's identity is its defining (primary) MeSH descriptor, not its
# display name: the LLM can word a name differently on any re-run, and keying on
# the name made every re-enrich insert a duplicate of each renamed condition.
EXISTING_BY_MESH = """
SELECT m.mesh_id, m.condition_id FROM condition_mesh m WHERE m.is_primary
"""

UPDATE_CONDITION = """
UPDATE condition SET
    name        = %(name)s,
    kind        = %(kind)s,
    description = coalesce(%(description)s, description),
    updated_at  = now()
    -- `status` untouched, as in UPSERT_CONDITION.
WHERE condition_id = %(condition_id)s
RETURNING condition_id
"""

UPSERT_MESH = """
INSERT INTO condition_mesh (condition_id, mesh_id, is_primary)
VALUES (%(condition_id)s, %(mesh_id)s, %(is_primary)s)
ON CONFLICT (condition_id, mesh_id) DO UPDATE SET is_primary = EXCLUDED.is_primary
"""

UPSERT_SYNONYM = """
INSERT INTO condition_synonym (condition_id, synonym, kind)
VALUES (%(condition_id)s, %(synonym)s, %(kind)s)
ON CONFLICT (condition_id, synonym) DO UPDATE SET kind = EXCLUDED.kind
"""

CHILDREN = """
SELECT mesh_id, child_mesh_id FROM staging.mesh_candidate_child WHERE mesh_id = ANY(%s)
"""

ENTRY_TERMS = """
SELECT mesh_id, name, entry_terms FROM staging.mesh_candidates WHERE mesh_id = ANY(%s)
"""


def _display_name(mesh_name: str, medline: dict | None, llm: dict | None) -> str:
    for candidate in (
        (medline or {}).get("title"),
        (llm or {}).get("display_name"),
        mesh_name,
    ):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return mesh_name


def _description(medline: dict | None, llm: dict | None) -> str | None:
    return (medline or {}).get("description") or (llm or {}).get("description")


def _synonyms(mesh_name: str, entry_terms: list, llm: dict | None) -> list[tuple[str, str]]:
    """(synonym, kind) pairs, lowercased and deduped.

    The descriptor's own name is included as a clinical synonym: a user who types
    the precise medical term should find the condition even when the display name
    is the friendlier one.
    """
    seen: dict[str, str] = {}

    def add(value: str, kind: str) -> None:
        text = (value or "").strip()
        if not text or len(text) > 120:
            return
        lowered = text.lower()
        # First writer wins: clinical/abbreviation labels are assigned from
        # authoritative MeSH terms and should not be overwritten by a generated
        # colloquial duplicate.
        seen.setdefault(lowered, kind)

    add(mesh_name, "clinical")
    for term in entry_terms or []:
        add(term, "abbreviation" if _ABBREVIATION.match(str(term).strip()) else "clinical")
    for term in (llm or {}).get("colloquial_synonyms", []):
        add(term, "colloquial")
    for term in (llm or {}).get("body_part_synonyms", []):
        add(term, "body_part")

    return sorted(seen.items())


def run(
    conn: psycopg.Connection,
    survivors: list[dict],
    medlineplus: dict,
    enrichment: dict,
) -> dict:
    """Load conditions, their MeSH mappings and their synonyms."""
    mesh_ids = [s["mesh_id"] for s in survivors]

    with conn.cursor() as cur:
        cur.execute(CHILDREN, (mesh_ids,))
        children: dict[str, list[str]] = {}
        for parent, child in cur.fetchall():
            children.setdefault(parent, []).append(child)

        cur.execute(ENTRY_TERMS, (mesh_ids,))
        terms = {row[0]: (row[1], row[2] or []) for row in cur.fetchall()}

        cur.execute(EXISTING_BY_MESH)
        existing = dict(cur.fetchall())

    stats = {"conditions": 0, "mesh_links": 0, "synonyms": 0, "name_collisions": 0,
             "skipped_not_searchable": 0}
    used_names: dict[str, str] = {}
    mesh_rows: list[dict] = []
    synonym_rows: list[dict] = []

    with conn.cursor() as cur:
        for survivor in survivors:
            mesh_id = survivor["mesh_id"]
            mesh_name, entry_terms = terms.get(mesh_id, (survivor["name"], []))
            llm = enrichment.get(mesh_id)
            medline = medlineplus.get(mesh_id)

            # The LLM is allowed to say "no patient would ever search this",
            # which is a genuinely useful filter the paper count cannot provide.
            if llm and llm.get("display_name") is None and not llm.get("colloquial_synonyms"):
                stats["skipped_not_searchable"] += 1
                continue

            name = _display_name(mesh_name, medline, llm)
            if name.lower() in used_names and used_names[name.lower()] != mesh_id:
                # `condition.name` is UNIQUE, and two descriptors can reduce to
                # the same friendly name. Qualify rather than drop one.
                name = f"{name} ({mesh_name})"
                stats["name_collisions"] += 1
            used_names[name.lower()] = mesh_id

            kind = (llm or {}).get("kind") or survivor.get("kind") or "condition"
            params = {
                "name": name,
                "kind": kind if kind in ("condition", "procedure") else "condition",
                "description": _description(medline, llm),
                "condition_id": existing.get(mesh_id),
            }
            if params["condition_id"] is None:
                cur.execute(UPSERT_CONDITION, params)
            else:
                try:
                    with conn.transaction():
                        cur.execute(UPDATE_CONDITION, params)
                except psycopg.errors.UniqueViolation:
                    # The new name belongs to another condition: qualify it.
                    params["name"] = f"{name} ({mesh_name})"
                    stats["name_collisions"] += 1
                    cur.execute(UPDATE_CONDITION, params)
            condition_id = cur.fetchone()[0]
            stats["conditions"] += 1

            # The descriptor that *defines* the condition is primary; everything
            # beneath it in the tree is a secondary mapping, so a paper tagged
            # with a narrower child still counts toward the parent.
            mesh_rows.append({"condition_id": condition_id, "mesh_id": mesh_id,
                              "is_primary": True})
            for child in children.get(mesh_id, []):
                mesh_rows.append({"condition_id": condition_id, "mesh_id": child,
                                  "is_primary": False})

            for synonym, synonym_kind in _synonyms(mesh_name, entry_terms, llm):
                synonym_rows.append({"condition_id": condition_id, "synonym": synonym,
                                     "kind": synonym_kind})

            if stats["conditions"] % 1000 == 0:
                conn.commit()
                log.info("  … %s conditions", f"{stats['conditions']:,}")
    conn.commit()

    stats["mesh_links"] = upsert(conn, UPSERT_MESH, mesh_rows)
    stats["synonyms"] = upsert(conn, UPSERT_SYNONYM, synonym_rows)

    analyze(conn, "condition", "condition_mesh", "condition_synonym")
    log.info(
        "loaded %s draft conditions, %s MeSH links, %s synonyms",
        f"{stats['conditions']:,}", f"{stats['mesh_links']:,}", f"{stats['synonyms']:,}",
    )
    return stats


def post_load_checks(conn: psycopg.Connection) -> list[str]:
    """The sanity checks from docs/prerequisites/05-condition-vocabulary.md."""
    warnings = []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM condition WHERE status = 'active'")
        active = cur.fetchone()[0]
        if active:
            # Only a review pass may promote. Anything already active before one
            # has run means the QA gate is being bypassed.
            warnings.append(f"{active:,} conditions are already 'active' before review")

        cur.execute(
            """
            SELECT count(*) FROM condition c
            WHERE (SELECT count(*) FROM condition_synonym s
                    WHERE s.condition_id = c.condition_id AND s.kind = 'colloquial') < 3
            """
        )
        thin = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM condition")
        total = cur.fetchone()[0]
        if total and thin / total > 0.25:
            warnings.append(
                f"{thin:,} of {total:,} conditions have fewer than 3 colloquial "
                "synonyms — batch results may not be joining back by custom_id"
            )

        cur.execute("SELECT count(*) FROM condition_mesh WHERE is_primary")
        primary = cur.fetchone()[0]
        if primary != total:
            warnings.append(
                f"{primary:,} primary MeSH links for {total:,} conditions — "
                "each condition should have exactly one"
            )
    return warnings
