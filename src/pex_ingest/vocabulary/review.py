"""Write a human review sheet for a run.

    pex vocab review --run R --groups urology        every concept in those groups
    pex vocab review --run R                         a stratified spot-check sample

The spot-check takes, from every generating group, its two biggest concepts by
Tier 1 papers, two from the middle and one near the keep threshold, so a reviewer
sees each specialty's head, body and tail without reading 15,000 cards. Output is
Markdown (review-<name>.md) for reading and CSV for marking verdicts.
"""

from __future__ import annotations

import csv
import json
import random

from ..config import Settings
from .merge import load_groups


def _row(item: dict, check: dict) -> dict:
    terms = item.get("terms") or []
    return {
        "name": item["name"], "kind": item["kind"],
        "groups": ", ".join(item["groups"]),
        "tier1_papers": check["tier1_papers"], "tier2_papers": check["tier2_papers"],
        "aliases": "; ".join(item.get("aliases", [])[:8]),
        "everyday": "; ".join(item["card"].get("everyday_names", [])[:3]),
        "tier1": "; ".join(t["term"] for t in terms if t["tier"] == 1 and not t["is_name"]),
        "tier2": "; ".join(t["term"] for t in terms if t["tier"] == 2),
        "links": "; ".join(f"{link['relation']}→{link['target']}"
                           for link in item["card"].get("links", [])),
        "support": ", ".join(f"{g} {s:.0%}" for g, s in
                             list(check.get("specialty_support", {}).items())[:3]),
        "changes": " ".join(check.get("specialty_changes", [])),
        "verdict": "", "note": "",
    }


def write(settings: Settings, run: str, groups_arg: str | None) -> dict:
    from .job import run_dir

    directory = run_dir(settings, run)
    final = json.loads((directory / "final.json").read_text(encoding="utf-8"))
    checks = {c["key"]: c for c in json.loads((directory / "checks.json").read_text())}

    if groups_arg:
        wanted = {g.strip() for g in groups_arg.split(",")}
        chosen = [i for i in final if wanted & set(i["groups"])]
        label = "-".join(sorted(wanted))
    else:
        rng = random.Random(20261011)
        chosen, seen = [], set()
        for group in load_groups():
            members = sorted((i for i in final if i["groups"] and i["groups"][0] == group.id),
                             key=lambda i: -checks[i["key"]]["tier1_papers"])
            if not members:
                continue
            middle = members[len(members) // 3: 2 * len(members) // 3]
            picks = members[:2] + rng.sample(middle, min(2, len(middle))) + members[-1:]
            for item in picks:
                if item["key"] not in seen:
                    seen.add(item["key"])
                    chosen.append(item)
        label = "spot-check"

    rows = [_row(i, checks[i["key"]]) for i in chosen]
    csv_path = directory / f"review-{label}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["name"])
        writer.writeheader()
        writer.writerows(rows)

    md = [f"# Review: {label} ({len(rows)} concepts, run {run})", ""]
    for r in rows:
        md.append(f"## {r['name']} ({r['kind']}) — T1 {r['tier1_papers']}, T2 {r['tier2_papers']}")
        md.append(f"- groups: {r['groups']} | support: {r['support']} {r['changes']}")
        md.append(f"- aliases: {r['aliases']} | everyday: {r['everyday']}")
        md.append(f"- T1: {r['tier1']}")
        md.append(f"- T2: {r['tier2']}")
        md.append(f"- links: {r['links']}")
        md.append("")
    md_path = directory / f"review-{label}.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    return {"concepts": len(rows), "markdown": str(md_path), "csv": str(csv_path)}
