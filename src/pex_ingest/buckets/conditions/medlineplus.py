"""MedlinePlus crosswalk: MeSH descriptor → patient-friendly name and summary.

MedlinePlus is NLM's consumer-health site, and its topic export names each topic
in language patients actually use ("Enlarged Prostate (BPH)" rather than
"Prostatic Hyperplasia"), with a plain-English summary. Each topic carries the
MeSH heading it corresponds to, which makes it a ready-made crosswalk.

It covers ~1,000 topics against ~6,000 kept descriptors, so it fills a minority —
that is expected, and it is precisely why the LLM pass exists. Where MedlinePlus
*does* have an answer it is preferred over a generated one: it is human-authored,
stable, and authoritative.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("pex.conditions.medlineplus")

_TAGS = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def _plain_text(value: str | None, limit: int = 400) -> str | None:
    """MedlinePlus summaries are HTML fragments; `condition.description` is a
    one-line patient-facing explanation, so this takes the first sentence or two.
    """
    if not value:
        return None
    text = _WHITESPACE.sub(" ", _TAGS.sub(" ", value)).strip()
    if len(text) <= limit:
        return text or None
    cut = text[:limit]
    stop = cut.rfind(". ")
    return (cut[: stop + 1] if stop > 100 else cut).strip() or None


def parse(path: Path) -> dict[str, dict]:
    """{mesh_id: {'title': …, 'description': …, 'url': …}}.

    Topics are keyed to MeSH by descriptor UI where the export provides one. Some
    topics carry several MeSH headings; the first is used, since the later ones
    are broader context rather than the topic's own subject.
    """
    from lxml import etree

    mapping: dict[str, dict] = {}
    for _event, topic in etree.iterparse(str(path), tag="health-topic"):
        title = topic.get("title")
        summary = topic.findtext("full-summary")
        url = topic.get("url")

        for mesh in topic.findall("mesh-heading/descriptor"):
            mesh_id = mesh.get("id")
            if not mesh_id or mesh_id in mapping:
                continue
            mapping[mesh_id] = {
                "title": title,
                "description": _plain_text(summary),
                "url": url,
            }
            break  # first heading only — later ones are broader context

        topic.clear()
        while topic.getprevious() is not None:
            del topic.getparent()[0]

    log.info("MedlinePlus: %s descriptors have a consumer-health topic", f"{len(mapping):,}")
    return mapping
