"""ORCID record parsing and the confirm / foreign verdict."""

from pex_ingest.buckets.publications.orcid_check import parse_record, verdict


def _record(employers, works=()):
    return {
        "person": {"name": {"given-names": {"value": "Charles"},
                            "family-name": {"value": "Bernstein"}}},
        "activities-summary": {
            "employments": {"affiliation-group": [
                {"summaries": [{"employment-summary": {
                    "organization": {"name": name, "address": {"country": country,
                                                               "region": region}},
                    "start-date": {"year": {"value": "1993"}},
                }}]} for name, country, region in employers
            ]},
            "works": {"group": [
                {"external-ids": {"external-id": [
                    {"external-id-type": kind, "external-id-value": value}]}}
                for kind, value in works
            ]},
        },
    }


def test_parse_record_keeps_affiliations_and_work_ids():
    parsed = parse_record(_record(
        [("University of Manitoba", "CA", "Man.")],
        [("doi", "https://doi.org/10.1/ABC"), ("pmid", "12345")],
    ))
    assert parsed["family"] == "Bernstein"
    assert parsed["affiliations"][0]["country"] == "CA"
    assert parsed["affiliations"][0]["start"] == 1993
    assert parsed["dois"] == ["10.1/abc"]
    assert parsed["pmids"] == ["12345"]


def test_verdicts():
    foreign = parse_record(_record([("University of Manitoba", "CA", "Man.")]))["affiliations"]
    us_ca = parse_record(_record([("UCLA", "US", "California")]))["affiliations"]
    assert verdict(foreign, "CA") == "foreign"
    assert verdict(us_ca, "CA") == "confirmed"
    assert verdict(us_ca, "NY") == "other_state"
    assert verdict([], "CA") == "no_info"


def test_orcid_first_name_must_agree():
    from pex_ingest.buckets.publications.orcid_check import first_names_agree

    us_ca = [{"type": "employment", "country": "US", "state": "CA"}]
    assert first_names_agree("J.", "john") is True
    assert first_names_agree("Jonathan Paul", "jon") is True
    assert first_names_agree("Marielle", "mindie") is False
    assert first_names_agree(None, "john") is None
    assert verdict(us_ca, "CA", "Mindie", "marielle") == "name_mismatch"
    assert verdict(us_ca, "CA", "Marielle H.", "marielle") == "confirmed"


def test_shared_author_keeps_a_clear_winner_only():
    from collections import defaultdict

    from pex_ingest.buckets.publications.matcher import demote_shared_authors

    rows = [
        {"author_id": "A1", "npi": "1", "status": "accepted", "confidence": 0.97},
        {"author_id": "A1", "npi": "2", "status": "accepted", "confidence": 0.85},
        {"author_id": "A2", "npi": "3", "status": "accepted", "confidence": 0.95},
        {"author_id": "A2", "npi": "4", "status": "accepted", "confidence": 0.93},
    ]
    stats = defaultdict(int, accepted=4)
    demote_shared_authors(rows, stats)
    assert [r["status"] for r in rows] == ["accepted", "held", "held", "held"]
