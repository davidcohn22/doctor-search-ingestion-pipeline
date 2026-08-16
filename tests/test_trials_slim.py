"""Slimming a ClinicalTrials.gov study into a trial row and its person mentions.

The structural risk this guards: a matcher that only reads `overallOfficials`
finds a handful of famous investigators and misses almost everyone, because most
physicians appear as a *site contact* on someone else's multi-site study.
"""

from pex_ingest.buckets.trials.slim import slim_study

STUDY = {
    "protocolSection": {
        "identificationModule": {
            "nctId": "NCT00046774",
            "briefTitle": "A Study of Something",
            "officialTitle": "A Randomized Study of Something",
        },
        "descriptionModule": {"briefSummary": "We are studying something."},
        "statusModule": {
            "overallStatus": "RECRUITING",
            "startDateStruct": {"date": "2024-03"},
            "primaryCompletionDateStruct": {"date": "2026-01-15"},
            "lastUpdatePostDateStruct": {"date": "2026-08-01"},
        },
        "designModule": {"phases": ["PHASE2", "PHASE3"]},
        "conditionsModule": {"conditions": ["Prostatic Hyperplasia"]},
        "armsInterventionsModule": {
            "interventions": [{"type": "DRUG", "name": "Tamsulosin", "description": "0.4mg"}]
        },
        "contactsLocationsModule": {
            "overallOfficials": [
                {
                    "name": "Sarfaraz A Hasni, M.D.",
                    "role": "PRINCIPAL_INVESTIGATOR",
                    "affiliation": "Cleveland Clinic",
                }
            ],
            "locations": [
                {
                    "facility": "Mount Sinai Hospital",
                    "city": "New York",
                    "state": "New York",
                    "contacts": [
                        {"name": "Jane Q. Roe, M.D.", "role": "PRINCIPAL_INVESTIGATOR"},
                        {"name": "Study Coordinator", "role": "CONTACT"},
                    ],
                }
            ],
        },
    },
    "derivedSection": {
        "conditionBrowseModule": {
            "meshes": [{"id": "D011470", "term": "Prostatic Hyperplasia"}]
        },
        "interventionBrowseModule": {"meshes": [{"id": "D058825", "term": "Tamsulosin"}]},
    },
}


def test_trial_fields():
    trial, _mentions = slim_study(STUDY)
    assert trial["nct_id"] == "NCT00046774"
    assert trial["brief_title"] == "A Study of Something"
    assert trial["overall_status"] == "RECRUITING"
    assert trial["phase"] == "PHASE2, PHASE3"
    assert trial["conditions"] == ["Prostatic Hyperplasia"]
    assert trial["condition_mesh_ids"] == ["D011470"]
    assert trial["intervention_mesh_ids"] == ["D058825"]
    assert trial["interventions"][0]["name"] == "Tamsulosin"


def test_month_only_dates_are_padded_not_dropped():
    """`trial.start_date` is a date column; "sometime in March 2024" still helps."""
    trial, _ = slim_study(STUDY)
    assert trial["start_date"] == "2024-03-01"
    assert trial["primary_completion_date"] == "2026-01-15"


def test_both_name_fields_are_read():
    _trial, mentions = slim_study(STUDY)
    fields = {m["source_field"] for m in mentions}
    assert fields == {"overall_official", "location_contact"}


def test_site_contacts_carry_their_site_geography():
    """Better matching evidence than an overall official's free-text affiliation."""
    _trial, mentions = slim_study(STUDY)
    site = next(m for m in mentions if m["source_field"] == "location_contact")
    assert site["facility"] == "Mount Sinai Hospital"
    assert site["city"] == "New York"
    assert site["state"] == "New York"


def test_administrative_contacts_are_excluded():
    """A study coordinator is a real person but not a physician investigator."""
    _trial, mentions = slim_study(STUDY)
    assert not any(m["name"] == "Study Coordinator" for m in mentions)


def test_mentions_carry_the_shared_name_key():
    _trial, mentions = slim_study(STUDY)
    keys = {m["name_key"] for m in mentions}
    assert keys == {"hasni|s", "roe|j"}


def test_a_study_without_an_nct_id_is_skipped_not_crashed():
    trial, mentions = slim_study({"protocolSection": {"identificationModule": {}}})
    assert trial == {} and mentions == []


def test_a_study_with_no_named_people_is_fine():
    minimal = {"protocolSection": {
        "identificationModule": {"nctId": "NCT1", "briefTitle": "T"},
        "statusModule": {"overallStatus": "COMPLETED"},
    }}
    trial, mentions = slim_study(minimal)
    assert trial["nct_id"] == "NCT1"
    assert mentions == []
    assert trial["condition_mesh_ids"] == []
