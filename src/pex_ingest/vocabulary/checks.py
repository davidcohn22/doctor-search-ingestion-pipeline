"""Check generated cards against the literature and against who actually publishes.

Runs locally against the pex database; costs nothing.

**Literature check.** Every card term is matched against all 5.7M paper titles.
  * A supporting term matching more than GENERIC_TITLE_SHARE of all titles means
    nothing on its own ("surgery" alone matched most titles in the benchmark) and
    is dropped. A concept's own names are exempt: they define it.
  * A concept needs at least MIN_TIER1_PAPERS papers whose title hits one of its
    Tier 1 terms (decided 2026-10-11: "a concept needs at least 10 Tier 1 papers").

**Term checks.** Two rules catch card terms that match the wrong papers:
  * off-specialty — a term whose papers are written by the concept's specialties
    far less often than papers naming the concept itself is dropped;
  * broader — a supporting Tier 1 term hitting several times more titles than the
    concept's own best name is demoted to Tier 2.

**MeSH check.** Suggested MeSH headings are resolved against the real MeSH file:
an ID is kept only when it exists and its name agrees with the suggestion; a
heading with no ID is resolved by exact name.

**Specialty check.** For each concept, which specialty groups' doctors write its
Tier 1 papers? A listed group whose doctors write almost none of them is dropped
(cecostomy under urology); a specific group whose doctors write a large share is
added. Both moves need enough authors to be meaningful.

Writes checks.json (per concept: decision, reasons, counts, terms dropped, MeSH
ids, specialty support) and final.json (the kept concepts, ready to load).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict

from ..config import Settings
from ..db import connect
from .corpus import term_hits_in_works
from .merge import is_abbreviation, load_groups
from .titles import TermIndex, normalize, term_key

log = logging.getLogger("pex.vocabulary.checks")

GENERIC_TITLE_SHARE = 0.005   # 0.5% of all titles ≈ 28,500: "breast cancer" scale is fine,
                              # "surgery", "children", "outcomes" are not
MIN_TIER1_PAPERS = 10
# Off-specialty terms: a term whose papers come from the concept's specialties far
# less often than papers on the concept's own name do is matching something else
# ("kidney injury" inside "acute kidney injury" for renal trauma; "MESA", the heart
# study, for sperm retrieval). Needs enough papers to judge.
OFF_TOPIC_RATIO = 0.35
OFF_TOPIC_MIN_PAPERS = 30
# A supporting Tier 1 term that hits several times more titles than the
# concept's own best name is broader than the concept ("bladder cancer" in the
# card for one bladder-tumor operation): it is demoted to Tier 2.
BROADER_RATIO = 4.0
BROADER_MIN_PAPERS = 300
SPECIALTY_MIN_AUTHORS = 20
SPECIALTY_DROP_SHARE = 0.03
SPECIALTY_ADD_SHARE = 0.25
# Generic groups are never auto-added: generalists publish on everything, and
# adding them would open nearly every concept's specialty filter to them.
NEVER_AUTO_ADD = {"internal-medicine", "family-medicine", "pediatrics", "general-surgery",
                  "emergency-medicine", "critical-care"}

_MESH_ID = re.compile(r"\b(D\d{6,9})\b")


def concept_terms(item: dict) -> list[tuple[str, int, bool]]:
    """(term, tier, is_name) for a concept: its names first, then the card's tiers.

    Names are the concept's name and every clinical alias, from both the listing
    and the card. Everyday phrasings are never title terms: "kidney problems" in a
    title says nothing about a specific condition.
    """
    card = item["card"]
    seen: dict[str, tuple[str, int, bool]] = {}

    def add(term: str, tier: int, is_name: bool) -> None:
        term = (term or "").strip()
        if not term:
            return
        key = normalize(term)
        if not key:
            return
        if key in seen:
            old = seen[key]
            if tier < old[1] or (tier == old[1] and is_name and not old[2]):
                seen[key] = (old[0], tier, is_name or old[2])
            return
        seen[key] = (term, tier, is_name)

    for name in [item["name"], *item.get("aliases", []), *card.get("aliases", [])]:
        add(name, 1, True)
    for term in card.get("tier1_terms", []):
        add(term, 1, False)
    for term in card.get("tier2_terms", []):
        add(term, 2, False)
    return list(seen.values())


def load_mesh(settings: Settings) -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    """({id: name}, {lowercase name or entry term: id}, {id: tree numbers})."""
    from ..buckets.conditions.mesh_parse import descriptors

    paths = sorted(settings.path("mesh").glob("*/desc*.gz"))
    if not paths:
        raise RuntimeError("no MeSH descriptor file under $PEX_DATA_DIR/mesh — run the MeSH parse")
    names, lookup, trees = {}, {}, {}
    for record in descriptors(paths[-1]):
        names[record["mesh_id"]] = record["name"]
        trees[record["mesh_id"]] = record["tree_numbers"]
        lookup.setdefault(record["name"].lower(), record["mesh_id"])
        for term in record["entry_terms"]:
            lookup.setdefault(term.lower(), record["mesh_id"])
    return names, lookup, trees


def resolve_mesh(headings: list[str], names: dict, lookup: dict) -> tuple[list[str], list[str]]:
    """(resolved ids, unresolved suggestions)."""
    resolved, unresolved = [], []
    for heading in headings or []:
        heading = heading.strip()
        found = _MESH_ID.search(heading)
        text = _MESH_ID.sub("", heading).strip(" ()-–:;,").lower()
        mesh_id = None
        if found and found.group(1) in names:
            candidate = found.group(1)
            # The ID must agree with the name, or the model guessed one of them.
            if not text or names[candidate].lower() == text or lookup.get(text) == candidate:
                mesh_id = candidate
        if mesh_id is None and text:
            mesh_id = lookup.get(text)
        if mesh_id:
            if mesh_id not in resolved:
                resolved.append(mesh_id)
        else:
            unresolved.append(heading)
    return resolved, unresolved


def fold_population_variants(final: list[dict]) -> list[dict]:
    """Fold "X in older men" into X when X is also a kept concept.

    The merge already does this for the population words it knows; this catches
    variants whose wording it didn't, after the fact, without regenerating cards.
    The variant's name becomes an alias of X and its groups are added to X's.
    """
    from .merge import population_base

    by_name = {normalize(item["name"]): item for item in final}
    kept = []
    for item in final:
        base_key = population_base(item["name"])
        base = by_name.get(base_key) if base_key else None
        if base is None or base is item:
            kept.append(item)
            continue
        base["aliases"] = sorted({*base.get("aliases", []), item["name"]}, key=str.lower)
        base["groups"] = base["groups"] + [g for g in item["groups"] if g not in base["groups"]]
    return kept


def _group_of_code(groups) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = defaultdict(set)
    for group in groups:
        for code in group.codes:
            mapping[code].add(group.id)
    return mapping


# For every paper, the specialty groups of its matched authors (accepted links,
# papers not set aside by the per-paper filter).
WORK_GROUP_CODES = """
SELECT a.openalex_work_id, array_agg(DISTINCT pt.taxonomy_code)
FROM authorship a
JOIN physician_author pa ON pa.npi = a.npi AND pa.openalex_author_id = a.openalex_author_id
JOIN physician_taxonomy pt ON pt.npi = a.npi
WHERE pa.status = 'accepted' AND a.excluded_reason IS NULL
GROUP BY 1
"""

WORK_AUTHORS = """
SELECT a.openalex_work_id, a.npi
FROM authorship a
JOIN physician_author pa ON pa.npi = a.npi AND pa.openalex_author_id = a.openalex_author_id
WHERE pa.status = 'accepted' AND a.excluded_reason IS NULL
  AND a.openalex_work_id = ANY(%s)
"""


def _work_groups(conn, groups) -> tuple[dict[str, int], list[str]]:
    """({work_id: bitmask of group indexes}, group ids by bit)."""
    order = [g.id for g in groups]
    bit = {g: 1 << i for i, g in enumerate(order)}
    code_bits: dict[str, int] = defaultdict(int)
    for code, group_ids in _group_of_code(groups).items():
        for group_id in group_ids:
            code_bits[code] |= bit[group_id]
    masks: dict[str, int] = {}
    with conn.cursor(name="work_groups") as cur:
        cur.itersize = 100_000
        cur.execute(WORK_GROUP_CODES)
        for work_id, codes in cur:
            mask = 0
            for code in codes:
                mask |= code_bits.get(code, 0)
            if mask:
                masks[work_id] = mask
    return masks, order


def _share(works, masks: dict[str, int], target: int) -> tuple[float, int]:
    """(share of grouped papers with an author in `target` groups, grouped papers)."""
    grouped = hits = 0
    for work_id in works:
        mask = masks.get(work_id)
        if mask:
            grouped += 1
            if mask & target:
                hits += 1
    return (hits / grouped if grouped else 0.0), grouped


def run(settings: Settings, run: str) -> dict:
    from .job import run_dir

    directory = run_dir(settings, run)
    items = json.loads((directory / "cards.json").read_text(encoding="utf-8"))
    groups = load_groups()
    group_by_id = {g.id: g for g in groups}

    # --- 1. index every term ----------------------------------------------------
    index = TermIndex()
    per_concept: list[list[dict]] = []
    for cid, item in enumerate(items):
        abbreviations = {normalize(a) for a in [item["name"], *item.get("aliases", []),
                                                *item["card"].get("aliases", [])]
                         if is_abbreviation(a)}
        rows = []
        for term, tier, is_name in concept_terms(item):
            abbrev = normalize(term) in abbreviations
            key = term_key(term, abbrev)
            if key is not None:
                index.add(cid, term, tier, is_name, abbrev)
            rows.append({"term": term, "tier": tier, "is_name": is_name,
                         "abbreviation": abbrev, "key": key})
        per_concept.append(rows)

    # --- 2. title hits per term key, and each paper's specialty groups --------------
    key_works: dict = defaultdict(list)
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM work")
            total_titles = cur.fetchone()[0]
        log.info("matching %s term keys against %s titles",
                 f"{len(index.phrases) + len(index.capitals):,}", f"{total_titles:,}")
        for work_id, keys in term_hits_in_works(conn, index):
            for key in keys:
                key_works[key].append(work_id)
        log.info("loading each paper's author specialties")
        masks, order = _work_groups(conn, groups)
    bit_of = {g: 1 << i for i, g in enumerate(order)}

    generic_limit = int(total_titles * GENERIC_TITLE_SHARE)
    generic_keys = {k for k, w in key_works.items() if len(w) > generic_limit}
    log.info("%d term keys match more than %s titles (generic unless a concept's own name)",
             len(generic_keys), f"{generic_limit:,}")

    # --- 3. term rules, then per-concept counts -------------------------------------
    mesh_names, mesh_lookup, _trees = load_mesh(settings)
    results = []
    tier1_works: dict[int, set] = {}
    for cid, item in enumerate(items):
        rows = per_concept[cid]
        listed = 0
        for group_id in item["groups"]:
            listed |= bit_of.get(group_id, 0)
        for row in rows:
            row["hits"] = len(key_works.get(row["key"], ())) if row["key"] is not None else 0
            row["status"] = "unmatchable" if row["key"] is None else "kept"
            if row["key"] in generic_keys and not row["is_name"]:
                row["status"] = "generic"

        # Reference: papers naming the concept by its canonical name, or by any of
        # its names when the canonical one is too rare to judge from.
        canonical = rows[0]
        name_rows = [r for r in rows if r["is_name"] and r["key"] is not None]
        ref_share, ref_n = _share(key_works.get(canonical["key"], ()), masks, listed) \
            if canonical["key"] is not None else (0.0, 0)
        if ref_n < OFF_TOPIC_MIN_PAPERS:
            pooled = {w for r in name_rows for w in key_works.get(r["key"], ())}
            ref_share, ref_n = _share(pooled, masks, listed)
        if ref_n >= OFF_TOPIC_MIN_PAPERS and listed:
            for row in rows[1:]:
                if row["status"] != "kept":
                    continue
                share, n = _share(key_works.get(row["key"], ()), masks, listed)
                row["specialty_share"] = round(share, 3)
                if n >= OFF_TOPIC_MIN_PAPERS and share < OFF_TOPIC_RATIO * ref_share:
                    row["status"] = "off_topic"

        best_name_hits = max((r["hits"] for r in name_rows if r["status"] == "kept"), default=0)
        for row in rows:
            if (row["status"] == "kept" and row["tier"] == 1 and not row["is_name"]
                    and row["hits"] >= BROADER_MIN_PAPERS
                    and row["hits"] > BROADER_RATIO * max(best_name_hits, 1)):
                row["tier"] = 2
                row["status"] = "demoted"

        t1, t2 = set(), set()
        for row in rows:
            if row["status"] in ("kept", "demoted"):
                (t1 if row["tier"] == 1 else t2).update(key_works.get(row["key"], ()))
        t2 -= t1
        tier1_works[cid] = t1
        mesh_ids, mesh_unresolved = resolve_mesh(item["card"].get("mesh_headings"),
                                                 mesh_names, mesh_lookup)
        results.append({
            "key": item["key"], "name": item["name"], "kind": item["kind"],
            "groups": item["groups"], "tier1_papers": len(t1), "tier2_papers": len(t2),
            "reference_share": round(ref_share, 3), "reference_papers": ref_n,
            "terms": [{k: v for k, v in r.items() if k != "key"} for r in rows],
            "dropped_generic_terms": [r["term"] for r in rows if r["status"] == "generic"],
            "off_topic_terms": [r["term"] for r in rows if r["status"] == "off_topic"],
            "demoted_terms": [r["term"] for r in rows if r["status"] == "demoted"],
            "mesh_ids": mesh_ids, "mesh_unresolved": mesh_unresolved,
        })

    # --- 4. specialty check -----------------------------------------------------
    code_groups = _group_of_code(groups)
    all_t1 = sorted({w for works in tier1_works.values() for w in works})
    work_npis: dict[str, set] = defaultdict(set)
    npi_groups: dict[str, set] = {}
    with connect(settings) as conn, conn.cursor() as cur:
        for start in range(0, len(all_t1), 200_000):
            cur.execute(WORK_AUTHORS, (all_t1[start:start + 200_000],))
            for work_id, npi in cur.fetchall():
                work_npis[work_id].add(npi)
        npis = sorted({n for s in work_npis.values() for n in s})
        for start in range(0, len(npis), 100_000):
            cur.execute("SELECT npi, taxonomy_code FROM physician_taxonomy WHERE npi = ANY(%s)",
                        (npis[start:start + 100_000],))
            for npi, code in cur.fetchall():
                npi_groups.setdefault(npi, set()).update(code_groups.get(code, ()))

    for cid, result in enumerate(results):
        authors = {n for w in tier1_works[cid] for n in work_npis.get(w, ())}
        with_group = [n for n in authors if npi_groups.get(n)]
        support: Counter = Counter()
        for npi in with_group:
            for group_id in npi_groups[npi]:
                support[group_id] += 1
        n = len(with_group)
        result["tier1_authors"] = n
        result["specialty_support"] = (
            {g: round(c / n, 3) for g, c in support.most_common(8)} if n else {})

        final_groups = list(result["groups"])
        changes = []
        if n >= SPECIALTY_MIN_AUTHORS:
            for group_id in list(final_groups):
                if len(final_groups) > 1 and support[group_id] / n < SPECIALTY_DROP_SHARE:
                    final_groups.remove(group_id)
                    changes.append(f"-{group_id}")
            for group_id, count in support.most_common():
                group = group_by_id.get(group_id)
                if (count / n >= SPECIALTY_ADD_SHARE and group_id not in final_groups
                        and group and group.generate and group_id not in NEVER_AUTO_ADD):
                    final_groups.append(group_id)
                    changes.append(f"+{group_id}")
            listed = set(result["groups"])
            listed_share = sum(1 for npi in with_group if npi_groups[npi] & listed) / n
            result["listed_group_share"] = round(listed_share, 3)
            if listed_share < 0.2:
                changes.append("mismatch")
        result["final_groups"] = final_groups
        result["specialty_changes"] = changes

        reasons = []
        if result["tier1_papers"] < MIN_TIER1_PAPERS:
            reasons.append(f"only {result['tier1_papers']} Tier 1 papers")
        result["keep"] = not reasons
        result["reasons"] = reasons

    (directory / "checks.json").write_text(json.dumps(results, indent=1, ensure_ascii=False),
                                           encoding="utf-8")
    final = []
    for cid, (item, result) in enumerate(zip(items, results, strict=True)):
        if not result["keep"]:
            continue
        final.append({
            **item, "groups": result["final_groups"], "mesh_ids": result["mesh_ids"],
            "terms": [
                {"term": r["term"], "tier": r["tier"], "is_name": r["is_name"],
                 "abbreviation": r["abbreviation"]}
                for r in per_concept[cid] if r["status"] in ("kept", "demoted")
            ],
        })
    final = fold_population_variants(final)
    (directory / "final.json").write_text(json.dumps(final, indent=1, ensure_ascii=False),
                                          encoding="utf-8")

    summary = {
        "concepts": len(results),
        "kept": sum(r["keep"] for r in results),
        "dropped_too_few_papers": sum(not r["keep"] for r in results),
        "generic_term_keys": len(generic_keys),
        "terms_generic": sum(len(r["dropped_generic_terms"]) for r in results),
        "terms_off_topic": sum(len(r["off_topic_terms"]) for r in results),
        "terms_demoted": sum(len(r["demoted_terms"]) for r in results),
        "specialty_changes": sum(bool(r["specialty_changes"]) for r in results),
        "specialty_mismatches": sum("mismatch" in r["specialty_changes"] for r in results),
        "mesh_resolved": sum(len(r["mesh_ids"]) for r in results),
        "mesh_unresolved": sum(len(r["mesh_unresolved"]) for r in results),
    }
    (directory / "checks.summary.json").write_text(json.dumps(summary, indent=1))
    log.info("checks: %s", summary)
    return summary
