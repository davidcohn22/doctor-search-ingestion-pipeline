"""The two generation prompts: list a specialty's concepts, then write each concept's card.

Both follow the tier definitions in the Conditions & Search Refactor:

  * Tier 1 (core) — the concept itself plus terms so tightly bound to it that a
    paper on them is evidence for it: its names, its defining treatments, the
    precise anatomy-plus-problem phrasing. "Won't get much better than this."
  * Tier 2 (broader field) — topics a level or two up. A great sign, not direct.
  * Tier 3 (specialty) — any paper in the specialty. Not written by the model:
    it comes from OpenAlex fields (specialty_field).

The UPJ obstruction and pyeloplasty cards are the worked examples, because they
are the running example the whole design was judged against.
"""

from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# Pass 1 — list the concepts a specialty group handles (Sonnet 5.5)
# ---------------------------------------------------------------------------

LISTING_SYSTEM = """You build the search vocabulary for a physician-finder used by \
patients and caregivers. A person types a condition or procedure ("doctors \
specializing in UPJ obstruction", "best surgeon for pyeloplasty") and the product \
ranks physicians by their published research and clinical trials on it.

Your job: list the conditions and procedures one specialty handles, named the way \
people search for them. The list becomes the set of searchable concepts, so it \
must be specific, complete and free of junk."""

LISTING_PROMPT = """Specialty group: {group}

List the conditions (diagnoses, diseases, clinical problems) and procedures \
(operations, interventional or office procedures, named treatment approaches) that \
physicians in this specialty group diagnose, treat or perform, and that a patient \
might search for when looking for a specialist.

How specific: the level at which a patient is referred and a specialist builds \
expertise. Include a broad named disease ("kidney stones") AND the distinct \
subtypes, presentations and procedures specialists tell apart ("staghorn calculi", \
"ureteroscopy", "percutaneous nephrolithotomy (PCNL)"). For example, urology must \
include "UPJ obstruction" and "pyeloplasty", not only "ureteral obstruction".

Naming: use the term a specialist would say to a patient, which is often an \
abbreviation-led clinical name ("UPJ obstruction", "BPH", "TURP"), not an indexing \
heading ("Ureteral Obstruction", "Prostatic Hyperplasia"). Put the other forms in \
aliases.

Leave out:
- whole fields or topics ("urologic surgery", "surgery", "after surgery", \
"kidney health"),
- bare anatomy ("kidney"), drugs and drug classes on their own, lab values, \
generic tests ("blood test"),
- symptoms, unless the symptom itself is a common reason to be referred to this \
specialty (e.g. "blood in the urine (hematuria)" for urology, "chronic cough" for \
pulmonology),
- umbrella "care" or "management" entries ("cancer survivorship care", "catheter \
management", "enhanced recovery after surgery") and surgical approaches on their \
own ("robotic surgery"),
- imaging studies and lab tests that another specialty performs or reads \
(CT urogram, renal scan, semen analysis, urine cytology). Keep procedures this \
specialty itself performs and patients seek it out for (cystoscopy, prostate \
biopsy, urodynamic testing),
- concepts this specialty only touches in passing (a urologist does not treat \
"heart failure"),
- the same concept split by patient group ("bladder stones in children", \
"artificial urinary sphincter in women") unless specialists treat that group's \
version as a distinct problem with its own literature (e.g. "posterior urethral \
valves" is distinct; "pediatric kidney stones" usually is not),
- supporting steps around a treatment (prophylactic antibiotics, catheter training, \
bedside tests such as a cough stress test).

Coverage: be exhaustive within the specialty. A broad specialty usually has \
150–300 concepts; a narrow subspecialty 40–150. Do not pad with near-duplicates: \
"robotic pyeloplasty" is an alias-level variant of "pyeloplasty", not a new concept, \
unless specialists treat it as a distinct procedure.

For each concept give:
- name: the search-facing name (Title case not required; keep standard \
capitalization of abbreviations).
- kind: "condition" or "procedure".
- aliases: other names for exactly the same thing: clinical synonyms, expansions, \
abbreviations and spelling variants (British spellings, "pelviureteric junction \
obstruction", "PUJ obstruction", "UPJO"). Never a broader or narrower condition \
("ureteral obstruction" is not an alias of "ureteral stricture"; "antenatal \
hydronephrosis" is not an alias of "hydronephrosis"), a different condition, a \
symptom, a specialty word ("urologist") or an everyday phrase.
- everyday_names: 0–3 plain-language phrasings a patient might type ("blockage \
where the kidney meets the ureter"). Empty when none is natural."""

LISTING_SCHEMA = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["condition", "procedure"]},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "everyday_names": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "kind", "aliases", "everyday_names"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["concepts"],
    "additionalProperties": False,
}


def listing_messages(group_name: str) -> dict:
    return {
        "system": LISTING_SYSTEM,
        "messages": [{"role": "user", "content": LISTING_PROMPT.format(group=group_name)}],
    }


# ---------------------------------------------------------------------------
# Pass 2 — write a card per concept (Haiku 5.5, ~10 concepts per call)
# ---------------------------------------------------------------------------

CARD_SYSTEM = """You write evidence cards for a physician-finder. Each card defines, \
for one searchable condition or procedure, which research papers count as evidence \
that a physician is an expert in it. Paper TITLES are matched against the card's \
terms, so terms must be phrases that actually appear in medical paper titles.

Three tiers of evidence:
- Tier 1 (core): the concept itself plus the terms so tightly bound to it that a \
paper on them is direct evidence for it: every name and abbreviation of the \
concept, its precise clinical sub-forms, and for a condition its defining \
treatments (the operations or procedures that exist mainly for it). A paper on a \
Tier 1 term "won't get much better" as evidence.
- Tier 2 (broader field): topics one or two levels up, plus closely related \
alternatives. A paper on them is a great sign but not direct evidence. Go up as \
far as the specific surgical or medical field (e.g. "robotic urologic surgery", \
"urologic surgery"), never to bare generic words.
- Tier 3 is "any paper in the specialty" and is computed separately. Do not write it.

Term rules (apply to both tiers):
- lowercase phrases of 1–6 words, written as they appear in titles; no wildcards, \
no plural-only variants (matching handles plurals).
- never a bare generic word or phrase that appears in thousands of unrelated \
titles: "surgery", "outcomes", "kidney", "children", "cancer", "treatment", \
"obstruction", "management", "minimally invasive surgery".
- an abbreviation only when it is unambiguous in medical titles ("upjo", "turp"); \
never two-letter abbreviations.
- Tier 1 includes the concept's name and all its aliases (spelled out and \
abbreviated).
- 4–25 Tier 1 terms and 3–12 Tier 2 terms is typical.

Links connect the card to OTHER concepts in the provided list only (copy the name \
exactly): treated_by (condition → its procedures/treatments), treats (procedure → \
the conditions it treats), broader (this concept → a broader concept), related \
(a close sibling a searcher might also want). Only link to names in the list.

MeSH: give the 0–3 MeSH descriptor headings (exact MeSH names, with their D-numbers \
if you know them) that best cover the concept. Leave empty rather than guess."""

UPJ_CARD = {
    "name": "UPJ obstruction",
    "description": "A blockage where the kidney's drainage funnel meets the ureter, "
                   "which keeps urine from leaving the kidney normally.",
    "aliases": ["ureteropelvic junction obstruction", "pelviureteric junction obstruction",
                "puj obstruction", "upjo", "upj obstruction"],
    "everyday_names": ["blockage where the kidney meets the ureter"],
    "tier1_terms": ["upj obstruction", "ureteropelvic junction obstruction",
                    "pelviureteric junction obstruction", "puj obstruction", "upjo",
                    "ureteropelvic junction", "pelviureteric junction", "pyeloplasty",
                    "dismembered pyeloplasty", "endopyelotomy", "ureterocalicostomy",
                    "balloon dilation of the ureteropelvic junction",
                    "crossing vessel"],
    "tier2_terms": ["ureteral stricture", "ureteral reconstruction",
                    "ureteroureterostomy", "hydronephrosis", "robotic urologic surgery",
                    "upper urinary tract reconstruction", "urologic surgery"],
    "links": [{"relation": "treated_by", "target": "Pyeloplasty"},
              {"relation": "treated_by", "target": "Endopyelotomy"},
              {"relation": "broader", "target": "Ureteral stricture"},
              {"relation": "related", "target": "Hydronephrosis"}],
    "mesh_headings": ["Ureteral Obstruction (D014517)"],
}

PYELOPLASTY_CARD = {
    "name": "Pyeloplasty",
    "description": "Surgery that removes the narrowed part where the kidney meets the "
                   "ureter and reconnects them so urine drains freely.",
    "aliases": ["dismembered pyeloplasty", "anderson-hynes pyeloplasty",
                "robotic pyeloplasty", "laparoscopic pyeloplasty"],
    "everyday_names": ["surgery to fix a kidney blockage"],
    "tier1_terms": ["pyeloplasty", "dismembered pyeloplasty", "anderson-hynes pyeloplasty",
                    "robotic pyeloplasty", "laparoscopic pyeloplasty",
                    "robot-assisted pyeloplasty", "upj obstruction",
                    "ureteropelvic junction obstruction",
                    "pelviureteric junction obstruction", "upjo"],
    "tier2_terms": ["endopyelotomy", "ureterocalicostomy", "ureteral reconstruction",
                    "ureteroureterostomy", "ureteral stricture",
                    "robotic urologic surgery", "urologic surgery"],
    "links": [{"relation": "treats", "target": "UPJ obstruction"},
              {"relation": "related", "target": "Endopyelotomy"}],
    "mesh_headings": [],
}

CARD_PROMPT = """Write one card for each concept below.

Worked examples (from urology). Note how the condition's card puts its defining \
operation in Tier 1, while the procedure's card puts its condition in Tier 1 and \
the alternative treatments in Tier 2, so the two searches rank similar but not \
identical doctors:

{examples}

Concepts that exist in this vocabulary and may be linked to (specialty: {groups}):
{neighbors}

Concepts to write cards for now (keep each "name" exactly as given):
{concepts}"""

CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "everyday_names": {"type": "array", "items": {"type": "string"}},
                    "tier1_terms": {"type": "array", "items": {"type": "string"}},
                    "tier2_terms": {"type": "array", "items": {"type": "string"}},
                    "links": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "relation": {
                                    "type": "string",
                                    "enum": ["treated_by", "treats", "broader", "related"],
                                },
                                "target": {"type": "string"},
                            },
                            "required": ["relation", "target"],
                            "additionalProperties": False,
                        },
                    },
                    "mesh_headings": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "description", "aliases", "everyday_names",
                             "tier1_terms", "tier2_terms", "links", "mesh_headings"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}


def card_messages(concepts: list[dict], neighbor_names: list[str], group_names: list[str]) -> dict:
    examples = "\n".join(json.dumps(c, ensure_ascii=False) for c in (UPJ_CARD, PYELOPLASTY_CARD))
    listed = "\n".join(
        json.dumps({"name": c["name"], "kind": c["kind"], "aliases": c["aliases"][:12]},
                   ensure_ascii=False)
        for c in concepts
    )
    return {
        "system": CARD_SYSTEM,
        "messages": [{
            "role": "user",
            "content": CARD_PROMPT.format(
                examples=examples,
                groups=", ".join(group_names),
                neighbors="; ".join(neighbor_names),
                concepts=listed,
            ),
        }],
    }
