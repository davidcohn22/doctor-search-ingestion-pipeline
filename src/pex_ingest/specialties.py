"""Specialty vocabulary: NUCC taxonomy text ↔ OpenAlex subfields.

Two matchers need to answer "does this author's research look like this doctor's
specialty?", and the two vocabularies don't line up: NPPES describes a physician
with a NUCC taxonomy string ("Internal Medicine, Cardiovascular Disease") while
OpenAlex describes an author with topic/subfield names ("Cardiology and
Cardiovascular Medicine"). This table is the bridge.

The keywords are matched case-insensitively as substrings, so they stay robust to
NUCC re-wording between its twice-yearly releases — the same approach, and the
same table, that scripts/bootstrap_eval_set.py already uses for the eval set.
Keeping one copy means a specialty added for matching is automatically available
to the eval tooling that measures it.
"""

from __future__ import annotations

# (OpenAlex subfield id, label, NUCC taxonomy keywords)
SPECIALTIES: list[tuple[str, str, list[str]]] = [
    ("2705", "Cardiology", ["cardiovascular disease", "cardiac", "cardiology"]),
    ("2730", "Oncology", ["oncology", "hematology & oncology"]),
    ("2746", "Surgery", ["surgery"]),
    ("2728", "Neurology", ["neurology", "neuromuscular"]),
    ("2708", "Dermatology", ["dermatology"]),
    ("2732", "Orthopedics", ["orthopaedic", "orthopedic", "sports medicine"]),
    ("2729", "Obstetrics & Gynecology", ["obstetrics", "gynecology"]),
    ("2735", "Pediatrics", ["pediatric"]),
    ("2738", "Psychiatry", ["psychiatry"]),
    ("2715", "Gastroenterology", ["gastroenterology"]),
    ("2748", "Urology", ["urology"]),
    ("2731", "Ophthalmology", ["ophthalmology"]),
    ("2712", "Endocrinology", ["endocrinology", "diabetes"]),
    ("2727", "Nephrology", ["nephrology"]),
    ("2740", "Pulmonology", ["pulmonary"]),
    ("2745", "Rheumatology", ["rheumatology"]),
    ("2703", "Anesthesiology", ["anesthesiology", "pain medicine"]),
    ("2741", "Radiology", ["radiology", "nuclear medicine", "radiation oncology"]),
    ("2725", "Infectious Disease", ["infectious disease"]),
    ("2720", "Hematology", ["hematology"]),
    ("2733", "Otolaryngology", ["otolaryngology"]),
    ("2723", "Allergy & Immunology", ["allergy", "immunology"]),
    ("2711", "Emergency Medicine", ["emergency medicine"]),
    ("2714", "Family Practice", ["family medicine"]),
    ("2724", "Internal Medicine", ["internal medicine"]),
    ("2717", "Geriatrics", ["geriatric"]),
    ("2706", "Critical Care", ["critical care", "intensive care"]),
    ("2747", "Transplantation", ["transplant"]),
    ("2739", "Pathology", ["pathology"]),
    ("2716", "Genetics", ["genetics", "genomics"]),
    ("2718", "Physical Medicine & Rehab", ["physical medicine", "rehabilitation"]),
    ("2713", "Preventive Medicine", ["preventive medicine", "public health"]),
]

# Order matters when labelling: the first keyword hit wins, so subspecialties
# must be tested before the generic parents they sit under. "Orthopaedic Surgery"
# must land in Orthopedics rather than Surgery, and "Internal Medicine,
# Nephrology" in Nephrology.
GENERIC_BUCKETS = {"Surgery", "Internal Medicine", "Family Practice", "Pediatrics"}

SUBFIELD_TO_LABEL = {sid: label for sid, label, _ in SPECIALTIES}


def label_for_taxonomy(taxonomy_text: str) -> str:
    """Bucket a NUCC taxonomy description into one of our specialty labels."""
    text = (taxonomy_text or "").lower()
    if not text:
        return "Unknown"
    specific = [s for s in SPECIALTIES if s[1] not in GENERIC_BUCKETS]
    generic = [s for s in SPECIALTIES if s[1] in GENERIC_BUCKETS]
    for _sid, label, keywords in specific + generic:
        if any(kw in text for kw in keywords):
            return label
    return (taxonomy_text or "").split(",")[0].strip() or "Unknown"


def labels_for_taxonomies(taxonomy_texts) -> set[str]:
    """Every specialty label a physician's taxonomy rows imply."""
    labels = {label_for_taxonomy(t) for t in taxonomy_texts if t}
    return {label for label in labels if label != "Unknown"}


def label_for_subfield(subfield_id: str) -> str | None:
    """OpenAlex subfield id → our specialty label, when we recognize it."""
    return SUBFIELD_TO_LABEL.get(str(subfield_id).rsplit("/", 1)[-1])


def specialty_fit(author_subfield_ids, physician_labels) -> float:
    """0–1: how well an author's research subfields match a doctor's specialty.

    Returns 0.0 when the author has no recognized medical subfield at all, which
    is the common case for the many non-clinical authors in the pool and is a
    genuinely useful negative signal. A partial score for a *related* specialty
    is deliberately not modelled here — the scorer treats specialty as one of
    several independent signals, so a near-miss shows up as this signal simply
    not firing rather than as a fudged fraction.
    """
    if not physician_labels:
        return 0.0
    author_labels = {label_for_subfield(s) for s in author_subfield_ids or []}
    author_labels.discard(None)
    if not author_labels:
        return 0.0
    overlap = author_labels & set(physician_labels)
    if not overlap:
        return 0.0
    # Full credit for any exact specialty agreement; a doctor publishing across
    # several of their own subfields is normal and shouldn't be penalized.
    return 1.0
