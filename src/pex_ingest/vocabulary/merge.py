"""Specialty groups, and merging the per-group concept lists into one vocabulary.

The same concept arrives from several groups (kidney stones from urology and
nephrology; "UPJ obstruction" from urology and pediatric urology). It becomes one
concept mapped to every group that listed it.

Two listings are the same concept when one's *name* equals the other's name or
one of its aliases, after normalization. Alias-to-alias matches are not enough:
two different conditions can share a loose alias, and merging them would blur
both cards.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import REPO_ROOT
from .titles import normalize

GROUPS_FILE = REPO_ROOT / "config" / "specialty_groups.yaml"


@dataclass
class Group:
    id: str
    name: str
    codes: list[str]
    subfields: list[str]
    generate: bool = True


def load_groups(path: Path = GROUPS_FILE) -> list[Group]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        Group(g["id"], g["name"], list(g["codes"]), [str(s) for s in g["subfields"]],
              bool(g.get("generate", True)))
        for g in data["groups"]
    ]


@dataclass
class Concept:
    """One merged concept, before its card is written."""

    key: str                                  # stable id: slug of the chosen name
    name: str
    kind: str
    aliases: list[str] = field(default_factory=list)
    everyday_names: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)   # in order of first listing

    def as_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "kind": self.kind,
                "aliases": self.aliases, "everyday_names": self.everyday_names,
                "groups": self.groups}


_ABBREV = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,7}$")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


def merge(listings: dict[str, list[dict]]) -> list[Concept]:
    """{group_id: [listed concept, …]} → merged concepts, deterministic order."""
    entries = []   # (group_id, listed concept)
    for group_id in listings:
        for item in listings[group_id]:
            name = (item.get("name") or "").strip()
            if name:
                entries.append((group_id, item))

    parent = list(range(len(entries)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # Merge rules (revised 2026-10-11 after the first full run, where chaining
    # merges through one-sided aliases folded adrenal tumors and glomus tumors into
    # pheochromocytoma, and UTI and interstitial cystitis into pyelonephritis):
    #   1. Same name (ignoring a trailing "(ABBR)") → same concept.
    #   2. An alias joins two listings only when it is mutual: each lists the
    #      other's name as an alias. One-sided aliases are often a broader or
    #      related term, so they never merge.
    #   3. A population variant ("Insomnia in children") folds into the base
    #      concept when one exists — one way only.
    # A missed merge leaves two similar concepts, which search can disambiguate;
    # a wrong merge destroys one, which nothing downstream can repair.
    by_name: dict[str, list[int]] = defaultdict(list)
    own_keys: list[set[str]] = []
    alias_keys: list[set[str]] = []
    for i, (_g, item) in enumerate(entries):
        keys = name_keys(item["name"])
        own_keys.append(keys)
        alias_keys.append({normalize(a) for a in item.get("aliases") or []
                           if len(normalize(a)) >= 5 and not is_abbreviation(a)})
        for key in keys:
            by_name[key].append(i)
    for indices in by_name.values():
        for i in indices[1:]:
            union(indices[0], i)
    for i in range(len(entries)):
        for key in alias_keys[i]:
            for j in by_name.get(key, ()):
                if j != i and alias_keys[j] & own_keys[i]:
                    union(i, j)
        base = population_base(entries[i][1]["name"])
        if base:
            for j in by_name.get(base, ()):
                union(i, j)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(entries)):
        clusters[find(i)].append(i)

    # Every cluster's own name keys. An alias that is some OTHER concept's name
    # ("pyelonephritis" listed under urinary tract infection) is a related concept,
    # not a synonym: kept as an alias it would resolve searches to the wrong
    # concept and count the other concept's papers as Tier 1 name hits.
    cluster_names: dict[int, set[str]] = defaultdict(set)
    for root, members in clusters.items():
        for i in members:
            cluster_names[root] |= own_keys[i]

    all_names: set[str] = set().union(*cluster_names.values()) if cluster_names else set()
    concepts: list[Concept] = []
    used_keys: set[str] = set()
    for root in sorted(clusters):
        members = clusters[root]
        names = Counter(entries[i][1]["name"].strip() for i in members)
        # Most often listed name wins; ties go to the shorter, then alphabetical.
        name = sorted(names, key=lambda n: (-names[n], len(n), n))[0]
        kinds = Counter(entries[i][1].get("kind", "condition") for i in members)
        kind = kinds.most_common(1)[0][0]

        aliases: dict[str, str] = {}
        everyday: dict[str, str] = {}
        other_names = all_names - cluster_names[root]
        name_norm = normalize(name)
        name_words = set(name_norm.split())
        for i in members:
            item = entries[i][1]
            for other in [item["name"], *(item.get("aliases") or [])]:
                other = other.strip()
                key = normalize(other)
                if not other or key == name_norm:
                    continue
                # A one-word alias that is just part of the name ("Shock" under
                # "Cardiogenic shock") is the broader condition: never a name.
                if len(key.split()) == 1 and broader_alias(key, name_words):
                    continue
                if key in other_names and key not in cluster_names[root]:
                    continue
                aliases.setdefault(key, other)
            for phrase in item.get("everyday_names") or []:
                phrase = phrase.strip()
                if phrase:
                    everyday.setdefault(normalize(phrase), phrase)

        groups: list[str] = []
        for i in members:
            if entries[i][0] not in groups:
                groups.append(entries[i][0])

        key = slug(name) or f"concept-{root}"
        while key in used_keys:
            key += "-x"
        used_keys.add(key)
        concepts.append(Concept(key, name, kind, sorted(aliases.values(), key=str.lower),
                                sorted(everyday.values(), key=str.lower), groups))
    return concepts


_PAREN = re.compile(r"\s*\([^)]*\)\s*")
# A concept split by patient group is the same concept: "Priapism in children" is
# priapism, and its card's Tier 1 terms are priapism's. The groups that listed it
# are kept, so pediatric urologists still pass its specialty filter.
_POPULATION_SUFFIX = re.compile(
    r"\s+(in|for|among)\s+(the\s+)?(children|adolescents|infants|neonates|newborns|women|"
    r"men|adults|older adults|older men|older women|older people|elderly|youth|young adults|"
    r"children and adolescents|adolescents and young adults|adolescent girls|"
    r"adolescent boys|girls|boys|obesity|infancy|childhood)$", re.I)


def name_keys(name: str) -> set[str]:
    """The normalized forms a listed name is known by: as written, and without a
    trailing "(ABBR)" — so "Overactive bladder (OAB)" and "Overactive bladder" meet."""
    base = _PAREN.sub(" ", name).strip()
    return {k for k in (normalize(name), normalize(base)) if k}


def population_base(name: str) -> str | None:
    """'Priapism in children' → 'priapism': the concept a population variant folds into."""
    base = _PAREN.sub(" ", name).strip()
    stripped = _POPULATION_SUFFIX.sub("", base).strip()
    return normalize(stripped) if stripped and stripped != base else None


def broader_alias(alias_key: str, name_words: set[str]) -> bool:
    """True when an alias's words are a strict subset of the name's words."""
    words = set(alias_key.split())
    return bool(words) and words < name_words


def is_abbreviation(text: str) -> bool:
    """'TURP', 'UPJO', 'PCNL' — an all-capitals token, as listed by the model."""
    return bool(_ABBREV.match(text.strip()))
