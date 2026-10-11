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
    ("2714", "Family Practice", ["family medicine", "general practice"]),
    ("2724", "Internal Medicine", ["internal medicine", "hospitalist"]),
    ("2717", "Geriatrics", ["geriatric"]),
    ("2706", "Critical Care", ["critical care", "intensive care"]),
    ("2747", "Transplantation", ["transplant"]),
    ("2734", "Pathology", ["pathology"]),
    ("2716", "Genetics", ["genetics", "genomics"]),
    ("2742", "Physical Medicine & Rehab", ["physical medicine", "rehabilitation"]),
    ("2713", "Preventive Medicine", ["preventive medicine", "public health"]),
]

# Order matters when labelling: the first keyword hit wins, so subspecialties
# must be tested before the generic parents they sit under. "Orthopaedic Surgery"
# must land in Orthopedics rather than Surgery, and "Internal Medicine,
# Nephrology" in Nephrology.
GENERIC_BUCKETS = {"Surgery", "Internal Medicine", "Family Practice", "Pediatrics"}

# Further OpenAlex subfields that count as research in a specialty. The table
# above holds each label's namesake subfield, but OpenAlex files only 2 topics
# under Family Practice and 1 under Internal Medicine, so generalists publish
# almost entirely elsewhere (public health, epidemiology), and a few specialties
# have a sibling subfield (hepatology, reproductive medicine). Without these the
# specialty signal could never fire for ~490K generalists, and their best
# possible score stayed below the accept threshold.
EXTRA_SUBFIELDS: dict[str, list[str]] = {
    "Family Practice": ["2724", "2739", "2713", "2717"],
    "Internal Medicine": ["2714", "2739", "2713", "2717"],
    "Preventive Medicine": ["2739"],
    "Gastroenterology": ["2721"],
    "Obstetrics & Gynecology": ["2743"],
}

SUBFIELD_TO_LABEL = {sid: label for sid, label, _ in SPECIALTIES}

SUBFIELD_TO_LABELS: dict[str, set[str]] = {}
for _sid, _label, _ in SPECIALTIES:
    SUBFIELD_TO_LABELS.setdefault(_sid, set()).add(_label)
for _label, _sids in EXTRA_SUBFIELDS.items():
    for _sid in _sids:
        SUBFIELD_TO_LABELS.setdefault(_sid, set()).add(_label)


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


def labels_for_subfield(subfield_id: str) -> set[str]:
    """Every specialty label an OpenAlex subfield counts toward (empty if none)."""
    return set(SUBFIELD_TO_LABELS.get(str(subfield_id).rsplit("/", 1)[-1], ()))


ADJACENT_CREDIT = 0.5


def specialty_fit(author_subfield_ids, physician_labels) -> float:
    """0–1: how well an author's research subfields match a doctor's specialty.

    1.0 when the author publishes in the specialty's own subfield (Urology →
    Urology). ADJACENT_CREDIT when the overlap is only through EXTRA_SUBFIELDS:
    public health or epidemiology topics are common to almost every medical
    author, so they corroborate a generalist far more weakly than a namesake
    subfield does. In a 30-accept review, full credit for them let three
    wrong-person links through (different first name, foreign researcher).
    0.0 when the author has no recognized medical subfield at all, the common
    case for the many non-clinical authors in the pool and a useful negative.
    """
    physician_labels = set(physician_labels or [])
    if not physician_labels:
        return 0.0
    subfields = list(author_subfield_ids or [])
    if {label_for_subfield(s) for s in subfields} & physician_labels:
        return 1.0
    adjacent = set()
    for subfield_id in subfields:
        adjacent |= labels_for_subfield(subfield_id)
    return ADJACENT_CREDIT if adjacent & physician_labels else 0.0
