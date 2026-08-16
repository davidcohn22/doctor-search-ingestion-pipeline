"""Parse the MeSH descriptor XML and select candidate conditions.

The source is one file — `desc<YEAR>.gz`, 16 MB gzipped / 298 MB raw — and it
carries everything this bucket needs: descriptor IDs, names, tree numbers, and
entry terms (MeSH's own synonyms).

**Parse it streaming.** `lxml.iterparse` with `rec.clear()` after each record
keeps memory flat; loading a 298 MB XML document as a tree consumes gigabytes for
no reason.

Selection is prefix matching on tree numbers, and it produces two things:

  * `staging.mesh_candidates` — descriptors inside a configured branch,
  * `staging.mesh_candidate_child` — descriptors sitting strictly *under* a kept
    descriptor, so a paper tagged with a narrower child still counts toward the
    parent condition. That is what stops a strict pruning threshold from losing
    as much as it appears to.
"""

from __future__ import annotations

import gzip
import logging
from collections.abc import Iterator
from pathlib import Path

import psycopg

from ...db import Jsonb, analyze, upsert
from .branch_config import BranchConfig

log = logging.getLogger("pex.conditions.mesh")

UPSERT_CANDIDATE = """
INSERT INTO staging.mesh_candidates (mesh_id, name, tree_numbers, entry_terms,
                                     specialty, kind, loaded_at)
VALUES (%(mesh_id)s, %(name)s, %(tree_numbers)s, %(entry_terms)s, %(specialty)s,
        %(kind)s, now())
ON CONFLICT (mesh_id) DO UPDATE SET
    name         = EXCLUDED.name,
    tree_numbers = EXCLUDED.tree_numbers,
    entry_terms  = EXCLUDED.entry_terms,
    kind         = EXCLUDED.kind,
    loaded_at    = now()
"""

UPSERT_CHILD = """
INSERT INTO staging.mesh_candidate_child (mesh_id, child_mesh_id)
VALUES (%(mesh_id)s, %(child_mesh_id)s)
ON CONFLICT (mesh_id, child_mesh_id) DO NOTHING
"""


def descriptors(path: Path) -> Iterator[dict]:
    """Stream every DescriptorRecord out of desc<YEAR>.gz (or .xml).

    Yields mesh_id, name, tree numbers and entry terms. `rec.clear()` plus
    dropping already-processed siblings is what keeps memory flat across ~30,000
    records — without the sibling cleanup, lxml still holds the whole parsed tree
    through the root element's child list.
    """
    from lxml import etree

    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as fh:
        for _event, record in etree.iterparse(fh, tag="DescriptorRecord"):
            mesh_id = record.findtext("DescriptorUI")
            name = record.findtext("DescriptorName/String")
            if mesh_id and name:
                yield {
                    "mesh_id": mesh_id,
                    "name": name,
                    "tree_numbers": [t.text for t in record.findall(".//TreeNumber") if t.text],
                    "entry_terms": sorted({
                        t.text for t in record.findall(".//Term/String") if t.text
                    }),
                }
            record.clear()
            while record.getprevious() is not None:
                del record.getparent()[0]


def select(path: Path, config: BranchConfig) -> tuple[list[dict], list[tuple[str, str]], int]:
    """(candidates, child links, total descriptors seen).

    Two passes' worth of work in one: keep the descriptors inside a configured
    branch, and remember every kept descriptor's tree numbers so children can be
    linked afterwards without re-reading the file.
    """
    total = 0
    kept: dict[str, dict] = {}
    all_trees: list[tuple[str, list[str]]] = []

    for descriptor in descriptors(path):
        total += 1
        trees = descriptor["tree_numbers"]
        all_trees.append((descriptor["mesh_id"], trees))

        kind = config.classify(trees)
        if kind is None or descriptor["mesh_id"] in config.excluded_descriptors:
            continue
        kept[descriptor["mesh_id"]] = {
            "mesh_id": descriptor["mesh_id"],
            "name": descriptor["name"],
            "tree_numbers": Jsonb(trees),
            "entry_terms": Jsonb(descriptor["entry_terms"]),
            # Under national scope no specialty is known at parse time — it is
            # derived later from who publishes on the descriptor.
            "specialty": None,
            "kind": kind,
        }

    config.validate_against(all_trees)

    # Child links: a descriptor sitting strictly under a kept descriptor's tree
    # number. Built from the prefix relation rather than a second XML pass.
    kept_trees = {
        mesh_id: [t for t in trees]
        for mesh_id, trees in all_trees
        if mesh_id in kept
    }
    prefix_index: dict[str, str] = {}
    for mesh_id, trees in kept_trees.items():
        for tree in trees:
            prefix_index[tree] = mesh_id

    children: set[tuple[str, str]] = set()
    for child_id, trees in all_trees:
        for tree in trees:
            parts = tree.split(".")
            # Walk up the path looking for the nearest kept ancestor.
            for depth in range(len(parts) - 1, 0, -1):
                ancestor = prefix_index.get(".".join(parts[:depth]))
                if ancestor and ancestor != child_id:
                    children.add((ancestor, child_id))
                    break

    log.info(
        "MeSH: %s descriptors parsed, %s kept as candidates, %s child links",
        f"{total:,}", f"{len(kept):,}", f"{len(children):,}",
    )
    if total < 25_000:
        raise RuntimeError(
            f"Only {total:,} descriptors parsed — expected ~30,000. The streaming "
            "parse is dropping records, or the file is truncated."
        )
    return list(kept.values()), sorted(children), total


def load(conn: psycopg.Connection, path: Path, config: BranchConfig) -> dict:
    candidates, children, total = select(path, config)

    written = upsert(conn, UPSERT_CANDIDATE, candidates)
    child_written = upsert(
        conn,
        UPSERT_CHILD,
        ({"mesh_id": parent, "child_mesh_id": child} for parent, child in children),
    )
    analyze(conn, "staging.mesh_candidates", "staging.mesh_candidate_child")

    by_kind: dict[str, int] = {}
    for candidate in candidates:
        by_kind[candidate["kind"]] = by_kind.get(candidate["kind"], 0) + 1
    log.info("candidates by kind: %s", by_kind)

    return {
        "descriptors_parsed": total,
        "candidates": written,
        "child_links": child_written,
        **{f"kind_{k}": v for k, v in by_kind.items()},
    }
