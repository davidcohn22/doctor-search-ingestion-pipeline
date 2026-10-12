"""Load a checked run (final.json) into the condition* tables, replacing the old vocabulary.

One transaction: readers see either the old vocabulary or the new one, never a
mix. Deleting the old `condition` rows cascades to its synonyms, MeSH links,
specialty links, terms, links and the derived feature rows, which the features
rebuild regenerates.

What goes where:

  condition           one row per concept, status 'active' (it passed the
                      literature checks and the review; there is no separate
                      promotion gate for generated concepts)
  condition_synonym   the name and clinical aliases (kind clinical, or
                      abbreviation when listed in capitals) and everyday phrasings
  condition_term      the checked Tier 1 / Tier 2 title terms; is_name marks the
                      concept's own names for the exact-name bonus
  condition_link      card links whose target is another loaded concept
  condition_taxonomy  every NUCC code of the concept's specialty groups;
                      is_primary on the first group's codes
  condition_mesh      validated MeSH ids (is_primary) plus their narrower
                      descriptors, all at Tier 2
  specialty_field     NUCC code ↔ OpenAlex subfield, from specialty_groups.yaml
"""

from __future__ import annotations

import bisect
import json
import logging
from collections import defaultdict

from ..config import Settings
from ..db import analyze, connect
from .checks import load_mesh
from .merge import is_abbreviation, load_groups
from .titles import normalize

log = logging.getLogger("pex.vocabulary.load")

MAX_MESH_DESCENDANTS = 50


def _mesh_descendants(trees: dict[str, list[str]]):
    """A function: MeSH id → the ids strictly beneath it in the tree."""
    by_tree = {}
    for mesh_id, numbers in trees.items():
        for number in numbers:
            by_tree[number] = mesh_id
    ordered = sorted(by_tree)

    def below(mesh_id: str) -> set[str]:
        out = set()
        for number in trees.get(mesh_id, []):
            prefix = number + "."
            # Tree numbers sort so that descendants follow their ancestor.
            i = bisect.bisect_left(ordered, prefix)
            while i < len(ordered) and ordered[i].startswith(prefix):
                out.add(by_tree[ordered[i]])
                i += 1
        out.discard(mesh_id)
        return out

    return below


def run(settings: Settings, run: str) -> dict:
    from .job import run_dir

    directory = run_dir(settings, run)
    final = json.loads((directory / "final.json").read_text(encoding="utf-8"))
    groups = {g.id: g for g in load_groups()}
    _names, _lookup, trees = load_mesh(settings)
    below = _mesh_descendants(trees)

    stats: defaultdict = defaultdict(int)
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT openalex_topic_id, subfield_id, subfield_name "
                        "FROM staging.openalex_topic")
            subfield_names = {sid: name for _t, sid, name in cur.fetchall() if sid}

            cur.execute("DELETE FROM condition")
            stats["old_conditions_removed"] = cur.rowcount
            cur.execute("DELETE FROM specialty_field")
            # The synonym-kind CHECK (migration 0009) is validated by migration
            # 0012 once these old rows are gone: VALIDATE needs the table owner.

            # --- concepts --------------------------------------------------------
            ids: dict[str, int] = {}
            by_name: dict[str, int] = {}
            used_names: set[str] = set()
            for item in final:
                name = item["name"].strip()
                if name.lower() in used_names:
                    name = f"{name} ({item['kind']})"
                used_names.add(name.lower())
                cur.execute(
                    "INSERT INTO condition (name, kind, description, status) "
                    "VALUES (%s, %s, %s, 'active') RETURNING condition_id",
                    (name, item["kind"], item["card"].get("description")),
                )
                cid = cur.fetchone()[0]
                ids[item["key"]] = cid
                for n in [item["name"], *item.get("aliases", [])]:
                    by_name.setdefault(normalize(n), cid)
            stats["conditions"] = len(ids)

            # --- synonyms, terms, links, taxonomy, MeSH ---------------------------------
            # A synonym that is another concept's own name ("inflammatory bowel
            # disease" as an everyday name of Crohn's disease) would make that name
            # ambiguous; it belongs to the other concept only.
            own_name = {normalize(item["name"]): ids[item["key"]] for item in final}
            synonyms, terms, links, taxonomy, mesh = [], [], set(), {}, {}
            for item in final:
                cid = ids[item["key"]]
                seen_syn: set[str] = {s for s, owner in own_name.items() if owner != cid}
                names = [item["name"], *item.get("aliases", []), *item["card"].get("aliases", [])]
                for text in names:
                    syn = " ".join(text.lower().split())
                    if syn and syn not in seen_syn and normalize(syn) not in seen_syn:
                        seen_syn.add(syn)
                        kind = "abbreviation" if is_abbreviation(text) else "clinical"
                        synonyms.append((cid, syn, kind))
                for text in [*item.get("everyday_names", []),
                             *item["card"].get("everyday_names", [])]:
                    syn = " ".join(text.lower().split())
                    if syn and syn not in seen_syn and normalize(syn) not in seen_syn:
                        seen_syn.add(syn)
                        synonyms.append((cid, syn, "everyday"))

                seen_term: dict[str, tuple] = {}
                for t in item["terms"]:
                    term = " ".join(t["term"].lower().split())
                    old = seen_term.get(term)
                    if old is None or t["tier"] < old[2]:
                        seen_term[term] = (cid, term, t["tier"], t["is_name"])
                terms.extend(seen_term.values())

                for link in item["card"].get("links", []):
                    target = by_name.get(normalize(link["target"]))
                    if target and target != cid:
                        links.add((cid, target, link["relation"]))
                    else:
                        stats["links_unresolved"] += 1

                for n, group_id in enumerate(item["groups"]):
                    for code in groups[group_id].codes:
                        taxonomy[(cid, code)] = taxonomy.get((cid, code), False) or n == 0

                for mesh_id in item.get("mesh_ids", []):
                    mesh[(cid, mesh_id)] = True
                    children = below(mesh_id)
                    if len(children) <= MAX_MESH_DESCENDANTS:
                        for child in children:
                            mesh.setdefault((cid, child), False)
                    else:
                        stats["mesh_headings_too_broad_to_expand"] += 1

            cur.executemany(
                "INSERT INTO condition_synonym (condition_id, synonym, kind) VALUES (%s, %s, %s)",
                synonyms)
            cur.executemany(
                "INSERT INTO condition_term (condition_id, term, tier, is_name) "
                "VALUES (%s, %s, %s, %s)", terms)
            cur.executemany(
                "INSERT INTO condition_link (from_id, to_id, relation) VALUES (%s, %s, %s)",
                sorted(links))
            cur.executemany(
                "INSERT INTO condition_taxonomy (condition_id, taxonomy_code, is_primary) "
                "VALUES (%s, %s, %s)", [(c, code, p) for (c, code), p in taxonomy.items()])
            cur.executemany(
                "INSERT INTO condition_mesh (condition_id, mesh_id, is_primary, tier) "
                "VALUES (%s, %s, %s, 2)", [(c, m, p) for (c, m), p in mesh.items()])

            fields = set()
            for group in groups.values():
                for code in group.codes:
                    for sid in group.subfields:
                        fields.add((code, sid, subfield_names.get(sid, sid)))
            cur.executemany(
                "INSERT INTO specialty_field (taxonomy_code, openalex_subfield_id, subfield_name) "
                "VALUES (%s, %s, %s)", sorted(fields))

            stats.update(synonyms=len(synonyms), terms=len(terms), links=len(links),
                         taxonomy_rows=len(taxonomy), mesh_rows=len(mesh),
                         specialty_field_rows=len(fields))
        conn.commit()
        analyze(conn, "condition", "condition_synonym", "condition_term", "condition_link",
                "condition_taxonomy", "condition_mesh", "specialty_field")
    log.info("vocabulary loaded: %s", dict(stats))
    return dict(stats)
