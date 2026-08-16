"""Slim a ClinicalTrials.gov study into a `trial` row plus its person mentions.

The structural fact to understand before reading this
(docs/prerequisites/04-clinical-trials.md):

  **Most physicians appear as a *site contact*, not as the national lead.**

  | field                  | who it is                                  | coverage        |
  |------------------------|--------------------------------------------|-----------------|
  | `overallOfficials`     | PI for the whole study, with an affiliation | rare            |
  | `locations[].contacts` | contact at one participating site           | **the common case** |

  "A matcher that only reads `overallOfficials` will find a handful of famous
   investigators and miss almost everyone."

The site contact is also the *better* evidence: it carries that site's facility,
city and state, which beats an overall official's free-text affiliation string
for geographic matching.

Pure functions with no I/O, so the parsing is unit-testable against a captured
study without a network or a database.
"""

from __future__ import annotations

from ...names import name_key

# Roles on a location contact that are administrative rather than clinical.
# A study coordinator is a real person but not a physician investigator, and
# admitting them adds noise to a matcher whose whole job is precision.
NON_INVESTIGATOR_ROLES = {"CONTACT", "CONTACT_BACKUP"}


def _mesh_ids(module: dict) -> list[str]:
    """MeSH descriptor IDs from a derivedSection browse module.

    These come from the same descriptor space as `work.mesh_terms`, which is what
    lets a condition search rank publications and trials on the same footing —
    one condition-relevance signal spanning both buckets.
    """
    return [m["id"] for m in (module or {}).get("meshes", []) if m.get("id")]


def slim_study(study: dict) -> tuple[dict, list[dict]]:
    """(trial row, person mentions) for one study."""
    proto = study.get("protocolSection") or {}
    derived = study.get("derivedSection") or {}

    ident = proto.get("identificationModule") or {}
    status_mod = proto.get("statusModule") or {}
    design = proto.get("designModule") or {}
    conditions_mod = proto.get("conditionsModule") or {}
    arms = proto.get("armsInterventionsModule") or {}
    contacts = proto.get("contactsLocationsModule") or {}

    nct_id = ident.get("nctId")
    if not nct_id:
        return {}, []

    phases = design.get("phases") or []
    trial = {
        "nct_id": nct_id,
        "brief_title": ident.get("briefTitle") or nct_id,
        "official_title": ident.get("officialTitle"),
        "brief_summary": (proto.get("descriptionModule") or {}).get("briefSummary"),
        "overall_status": status_mod.get("overallStatus"),
        "phase": ", ".join(phases) if phases else None,
        "conditions": conditions_mod.get("conditions") or [],
        "condition_mesh_ids": _mesh_ids(derived.get("conditionBrowseModule")),
        "interventions": [
            {
                "type": i.get("type"),
                "name": i.get("name"),
                "description": i.get("description"),
            }
            for i in (arms.get("interventions") or [])
        ],
        "intervention_mesh_ids": _mesh_ids(derived.get("interventionBrowseModule")),
        "start_date": _date(status_mod.get("startDateStruct")),
        "primary_completion_date": _date(status_mod.get("primaryCompletionDateStruct")),
        "last_update_posted": _date(status_mod.get("lastUpdatePostDateStruct")),
    }

    mentions = []
    for official in contacts.get("overallOfficials") or []:
        name = (official.get("name") or "").strip()
        if not name:
            continue
        mentions.append({
            "name": name,
            "nct_id": nct_id,
            "source_field": "overall_official",
            "role": official.get("role"),
            "affiliation": official.get("affiliation"),
            "facility": None,
            "city": None,
            "state": None,
            "name_key": name_key(name),
        })

    for location in contacts.get("locations") or []:
        for contact in location.get("contacts") or []:
            name = (contact.get("name") or "").strip()
            role = (contact.get("role") or "").upper()
            if not name or role in NON_INVESTIGATOR_ROLES:
                continue
            mentions.append({
                "name": name,
                "nct_id": nct_id,
                "source_field": "location_contact",
                "role": contact.get("role"),
                "affiliation": None,
                "facility": location.get("facility"),
                "city": location.get("city"),
                "state": location.get("state"),
                "name_key": name_key(name),
            })

    return trial, mentions


def _date(struct: dict | None) -> str | None:
    """ClinicalTrials.gov dates are 'YYYY-MM' or 'YYYY-MM-DD'.

    A month-only date is padded to the first of the month rather than dropped:
    `trial.start_date` is a `date` column, and knowing a trial started sometime
    in March 2024 is far more useful than knowing nothing.
    """
    if not struct:
        return None
    value = struct.get("date")
    if not value:
        return None
    if len(value) == 4:
        return f"{value}-01-01"
    if len(value) == 7:
        return f"{value}-01"
    return value
