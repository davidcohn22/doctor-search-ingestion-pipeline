"""`pex vocab …` — generate, check, review and load the concept vocabulary.

    pex vocab list     --run R [--groups a,b] [--live]   pass 1: concepts per group
    pex vocab merge    --run R                           one concept per real thing
    pex vocab cards    --run R [--live]                  pass 2: a card per concept
    pex vocab assemble --run R                           cards joined to concepts
    pex vocab check    --run R                           literature + specialty checks
    pex vocab review   --run R --groups a,b              a reviewable sheet
    pex vocab load     --run R                           replace the condition* tables

Everything for one run lives in $PEX_DATA_DIR/vocabulary/<run>/, so a tuning run
on urology and the full national run never mix, and any loaded concept can be
traced back to the reply that produced it.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path

from ..config import Settings
from . import generate
from .merge import Concept, load_groups, merge
from .titles import normalize

log = logging.getLogger("pex.vocabulary")


def run_dir(settings: Settings, run: str) -> Path:
    path = settings.path("vocabulary", run)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _selected_groups(groups_arg: str | None):
    groups = [g for g in load_groups() if g.generate]
    if groups_arg:
        wanted = {g.strip() for g in groups_arg.split(",") if g.strip()}
        unknown = wanted - {g.id for g in groups}
        if unknown:
            raise SystemExit(f"unknown or non-generating groups: {', '.join(sorted(unknown))}")
        groups = [g for g in groups if g.id in wanted]
    return groups


def _save_selection(directory: Path, groups) -> None:
    (directory / "groups.json").write_text(json.dumps([g.id for g in groups]), encoding="utf-8")


def _load_selection(directory: Path) -> list[str]:
    return json.loads((directory / "groups.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# pass 1
# ---------------------------------------------------------------------------

def list_concepts(settings: Settings, run: str, groups_arg: str | None, live: bool) -> dict:
    settings.require("anthropic_api_key")
    directory = run_dir(settings, run)
    groups = _selected_groups(groups_arg)
    _save_selection(directory, groups)
    jobs = {g.id: generate.listing_params(g.name) for g in groups}
    out = directory / "listing"
    if live:
        stats = generate.run_live(jobs, out, workers=6)
    else:
        stats = generate.run_batch(jobs, out, directory / "listing.batch.json")
    stats["cost_usd"] = round(generate.usage_cost(out, generate.LISTING_MODEL, not live), 3)
    log.info("listing: %s", stats)
    return stats


def merge_concepts(settings: Settings, run: str) -> dict:
    directory = run_dir(settings, run)
    listings: dict[str, list[dict]] = {}
    problems = {}
    for group_id in _load_selection(directory):
        path = directory / "listing" / f"{group_id}.json"
        if not path.exists():
            problems[group_id] = "missing"
            continue
        data, problem = generate.read_reply(path)
        if problem:
            problems[group_id] = problem
            continue
        listings[group_id] = data["concepts"]
    if problems:
        log.warning("listing problems: %s", problems)
    concepts = merge(listings)
    (directory / "concepts.json").write_text(
        json.dumps([c.as_dict() for c in concepts], indent=1, ensure_ascii=False),
        encoding="utf-8")
    listed = sum(len(v) for v in listings.values())
    log.info("merged %s listed concepts from %d groups into %s concepts",
             f"{listed:,}", len(listings), f"{len(concepts):,}")
    return {"groups": len(listings), "listed": listed, "concepts": len(concepts),
            "problems": problems}


def _load_concepts(directory: Path) -> list[Concept]:
    return [Concept(**c) for c in json.loads((directory / "concepts.json").read_text())]


# ---------------------------------------------------------------------------
# pass 2
# ---------------------------------------------------------------------------

MAX_NEIGHBORS = 450


def _card_chunks(concepts: list[Concept]) -> dict[str, list[Concept]]:
    """Concepts grouped by their first group, ~CARDS_PER_CALL per call."""
    by_group: dict[str, list[Concept]] = defaultdict(list)
    for concept in concepts:
        by_group[concept.groups[0]].append(concept)
    chunks = {}
    size = generate.CARDS_PER_CALL
    for group_id, members in by_group.items():
        for n in range(0, len(members), size):
            chunks[f"{group_id}--{n // size:03d}"] = members[n:n + size]
    return chunks


def write_cards(settings: Settings, run: str, live: bool) -> dict:
    settings.require("anthropic_api_key")
    directory = run_dir(settings, run)
    concepts = _load_concepts(directory)
    group_names = {g.id: g.name for g in load_groups()}

    members_of: dict[str, list[str]] = defaultdict(list)
    for concept in concepts:
        for group_id in concept.groups:
            members_of[group_id].append(concept.name)

    jobs = {}
    chunk_index = {}
    for custom_id, chunk in _card_chunks(concepts).items():
        groups = sorted({g for c in chunk for g in c.groups})
        neighbors: list[str] = []
        for group_id in groups:
            for name in members_of[group_id]:
                if name not in neighbors:
                    neighbors.append(name)
        jobs[custom_id] = generate.card_params(
            [c.as_dict() for c in chunk], neighbors[:MAX_NEIGHBORS],
            [group_names[g] for g in groups],
        )
        chunk_index[custom_id] = [c.key for c in chunk]
    (directory / "card_chunks.json").write_text(json.dumps(chunk_index, indent=1))

    out = directory / "cards"
    if live:
        stats = generate.run_live(jobs, out, workers=8)
    else:
        stats = generate.run_batch(jobs, out, directory / "cards.batch.json")
    stats["calls"] = len(jobs)
    stats["cost_usd"] = round(generate.usage_cost(out, generate.CARD_MODEL, not live), 3)
    log.info("cards: %s", stats)
    return stats


def assemble(settings: Settings, run: str) -> dict:
    """Join each saved card to its concept by name; report what's missing."""
    directory = run_dir(settings, run)
    concepts = {c.key: c for c in _load_concepts(directory)}
    chunks = json.loads((directory / "card_chunks.json").read_text())

    assembled = []
    stats: defaultdict = defaultdict(int)
    for custom_id, keys in chunks.items():
        path = directory / "cards" / f"{custom_id}.json"
        if not path.exists():
            stats["chunks_missing"] += 1
            stats["concepts_without_card"] += len(keys)
            continue
        data, problem = generate.read_reply(path)
        if problem:
            stats[f"chunks_{problem}"] += 1
            stats["concepts_without_card"] += len(keys)
            continue
        cards = {normalize(card["name"]): card for card in data["cards"]}
        for key in keys:
            concept = concepts[key]
            card = cards.get(normalize(concept.name))
            if card is None:
                stats["concepts_without_card"] += 1
                continue
            assembled.append({**concept.as_dict(), "card": card})
            stats["assembled"] += 1

    (directory / "cards.json").write_text(
        json.dumps(assembled, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("assembled: %s", dict(stats))
    return dict(stats)
