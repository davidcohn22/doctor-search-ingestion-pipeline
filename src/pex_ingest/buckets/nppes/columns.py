"""Header resolution for the NPPES bulk file.

The file has ~330 columns and **the header spellings differ between the V1 and V2
layouts** — V1 used long parenthetical names like `Provider Last Name (Legal
Name)`. The prerequisites doc is explicit that they must be verified against the
header row of the file actually downloaded rather than trusted from any doc:

  "Use the V2 layout — mandatory since 2026-03-03. Column names differ from V1,
   so verify header spellings against the header row of the file you actually
   downloaded."  — docs/prerequisites/01-nppes-identity.md

So resolution happens once, here, and everything downstream refers to stable
snake_case names. The rule is one normalization, not a list of hardcoded
spellings: lowercase, drop any parenthetical, collapse punctuation to
underscores. That turns both

    "Provider Last Name (Legal Name)"                              (V1)
    "Provider Last Name"                                           (V2)

into `provider_last_name` without needing to know which layout arrived.

A missing *required* column fails the run loudly. A silent mis-parse of a
330-column file is entirely possible and produces a plausible-looking, wrong
database — which is far more expensive than a job that refuses to start.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("pex.nppes.columns")

TAXONOMY_SLOTS = 15  # the file carries 15 taxonomy slots per provider

_PAREN = re.compile(r"\([^)]*\)")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize_header(header: str) -> str:
    """'Provider Last Name (Legal Name)' → 'provider_last_name'."""
    text = _PAREN.sub(" ", header.strip().lower())
    text = _NON_WORD.sub("_", text)
    return text.strip("_")


# Known spellings that don't reduce to the canonical name by rule alone.
ALIASES = {
    "provider_business_practice_location_address_state_name": "practice_state",
    "provider_business_practice_location_address_city_name": "practice_city",
    "provider_business_practice_location_address_postal_code": "practice_zip",
    "provider_business_practice_location_address_country_code": "practice_country",
    "provider_first_line_business_practice_location_address": "practice_line1",
    "provider_second_line_business_practice_location_address": "practice_line2",
    "provider_business_mailing_address_state_name": "mailing_state",
    "provider_business_mailing_address_city_name": "mailing_city",
    "provider_business_mailing_address_postal_code": "mailing_zip",
    "provider_first_line_business_mailing_address": "mailing_line1",
    "provider_second_line_business_mailing_address": "mailing_line2",
    "provider_last_name": "last_name",
    "provider_first_name": "first_name",
    "provider_middle_name": "middle_name",
    "provider_name_suffix_text": "name_suffix",
    "provider_name_prefix_text": "name_prefix",
    "provider_credential_text": "credential",
    "provider_gender_code": "gender",
    "provider_enumeration_date": "enumeration_date",
    "last_update_date": "last_update_date",
    "npi_deactivation_date": "deactivation_date",
    "npi_reactivation_date": "reactivation_date",
    "entity_type_code": "entity_type_code",
    "is_sole_proprietor": "sole_proprietor",
    "npi": "npi",
    "provider_other_last_name": "other_last_name",
    "provider_other_first_name": "other_first_name",
    "provider_other_middle_name": "other_middle_name",
    "provider_other_credential_text": "other_credential",
    "provider_other_last_name_type_code": "other_last_name_type",
}

# Without these, the two v1 filters ("is it a human?", "is it a doctor?") or the
# primary key cannot be applied at all.
REQUIRED = {"npi", "entity_type_code", "last_name", "first_name"}

# Per-slot fields, resolved for slots 1..15.
SLOT_ALIASES = {
    "healthcare_provider_taxonomy_code_{n}": "taxonomy_code_{n}",
    "provider_license_number_{n}": "license_number_{n}",
    "provider_license_number_state_code_{n}": "license_state_{n}",
    "healthcare_provider_primary_taxonomy_switch_{n}": "taxonomy_primary_{n}",
}


def resolve(headers: list[str]) -> dict[str, str]:
    """Map our canonical names → the file's actual header strings.

    Returns e.g. {'npi': 'NPI', 'last_name': 'Provider Last Name (Legal Name)',
                  'taxonomy_code_1': 'Healthcare Provider Taxonomy Code_1', …}
    """
    by_normal = {}
    for header in headers:
        by_normal.setdefault(normalize_header(header), header)

    resolved: dict[str, str] = {}
    for normal, canonical in ALIASES.items():
        if normal in by_normal:
            resolved[canonical] = by_normal[normal]

    for slot in range(1, TAXONOMY_SLOTS + 1):
        for pattern, canonical in SLOT_ALIASES.items():
            normal = pattern.format(n=slot)
            if normal in by_normal:
                resolved[canonical.format(n=slot)] = by_normal[normal]

    missing = REQUIRED - resolved.keys()
    if missing:
        raise RuntimeError(
            "NPPES header resolution failed — missing required columns: "
            f"{sorted(missing)}.\n"
            "The layout changed. Compare the file's header row against "
            "src/pex_ingest/buckets/nppes/columns.py:ALIASES.\n"
            f"First 20 headers seen: {headers[:20]}"
        )

    slots_found = sum(1 for s in range(1, TAXONOMY_SLOTS + 1) if f"taxonomy_code_{s}" in resolved)
    if slots_found < TAXONOMY_SLOTS:
        # Not fatal, but load-bearing: the "is this a physician?" filter tests
        # every slot, and only checking slot 1 collapses the spine from ~1M rows
        # to ~200K — a failure that looks like a successful load.
        log.warning(
            "only %d of %d taxonomy slots resolved; the physician filter will be "
            "narrower than intended",
            slots_found, TAXONOMY_SLOTS,
        )
    log.info("resolved %d NPPES columns (%d taxonomy slots)", len(resolved), slots_found)
    return resolved


def taxonomy_filter_sql(resolved: dict[str, str]) -> str:
    """The 15-slot "at least one taxonomy code starts with 20" clause.

    `20` is the NUCC range for Allopathic & Osteopathic Physicians — MDs and DOs.
    Generated rather than typed out, because hand-writing fifteen near-identical
    clauses is exactly how only slot 1 ends up being checked.
    """
    slots = [
        f'"{resolved[f"taxonomy_code_{n}"]}" LIKE \'20%\''
        for n in range(1, TAXONOMY_SLOTS + 1)
        if f"taxonomy_code_{n}" in resolved
    ]
    if not slots:
        raise RuntimeError("No taxonomy code columns resolved — cannot filter to physicians.")
    return "(" + " OR ".join(slots) + ")"


def projection_sql(resolved: dict[str, str]) -> str:
    """`SELECT "actual header" AS canonical_name, …` for the staging load."""
    return ",\n    ".join(
        f'"{actual}" AS {canonical}' for canonical, actual in sorted(resolved.items())
    )
