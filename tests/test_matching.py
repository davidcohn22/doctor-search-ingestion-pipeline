"""Thresholds and decisions — the rules that decide what reaches a profile.

The asymmetry these encode: a missing paper is a small gap nobody notices, a
wrong paper is the most credibility-destroying bug the product can ship. So these
tests are mostly about what must *not* be accepted.
"""

from pex_ingest.matching import (
    ACCEPT,
    HOLD,
    MIN_MARGIN,
    ScoreCard,
    decide,
    geo_overlap,
    middle_initial_agreement,
    resolve,
    summarize_thresholds,
)


def test_scorecard_weights_and_signal_trail():
    card = ScoreCard()
    card.add("name", 1.0, 0.30)
    card.add("state", 1.0, 0.30)
    assert card.final() == 0.60
    # The trail is what gets written to the `signals` jsonb column.
    assert card.signals == {"name": 1.0, "state": 1.0}


def test_a_signal_that_does_not_fire_leaves_no_trace():
    card = ScoreCard()
    card.add("state", 0.0, 0.30)
    assert card.final() == 0.0
    assert "state" not in card.signals


def test_penalties_are_recorded_as_negative():
    card = ScoreCard()
    card.add("name", 1.0, 0.90)
    card.penalize("middle_conflict", 0.15)
    assert card.final() == 0.75
    assert card.signals["middle_conflict"] == -0.15


def test_orcid_override_floors_but_never_lowers():
    card = ScoreCard()
    card.add("name", 1.0, 0.50)
    card.override("orcid_confirmed", 0.97)
    assert card.final() == 0.97

    strong = ScoreCard()
    strong.add("name", 1.0, 0.99)
    strong.override("orcid_confirmed", 0.97)
    assert strong.final() == 0.99  # confirmation must not demote a stronger score


def test_score_is_capped_below_certainty():
    card = ScoreCard()
    for i in range(10):
        card.add(f"s{i}", 1.0, 0.5)
    assert card.final() == 0.99  # nothing here is ever certain


def test_decide_bands():
    assert decide(0.95, margin=0.5) == "accepted"
    assert decide(0.75, margin=0.5) == "held"
    assert decide(0.10, margin=0.5) is None
    assert decide(HOLD, margin=1.0) == "held"
    assert decide(ACCEPT, margin=1.0) == "accepted"


def test_a_high_score_with_no_margin_is_held_not_accepted():
    """Two equally plausible same-named doctors must never be guessed between."""
    assert decide(0.95, margin=MIN_MARGIN / 2) == "held"


def test_resolve_accepts_a_clear_winner():
    results = resolve("1234567890", [
        ("A1", 0.95, {"name": 1.0}),
        ("A2", 0.40, {"name": 0.5}),
    ])
    assert [(r.external_id, r.status) for r in results] == [("A1", "accepted")]
    # The runner-up scored below the hold band, so it is dropped entirely rather
    # than stored as 'rejected' — that status is reserved for human verdicts.


def test_resolve_holds_when_a_rival_is_almost_as_good():
    """0.95 looks acceptable until you see the 0.89 sitting right behind it.

    The rival is below the accept threshold, so it is a genuine alternative
    reading of the same identity rather than another fragment of the same person
    — and 0.06 of daylight is not enough to choose between them.
    """
    results = resolve("1234567890", [
        ("A1", 0.95, {}),
        ("A2", 0.89, {}),
    ])
    statuses = {r.external_id: r.status for r in results}
    assert statuses["A1"] == "held"
    assert statuses["A2"] == "held"


def test_resolve_allows_several_accepted_identities():
    """OpenAlex fragments one human across entities; re-uniting them is the point."""
    results = resolve("1234567890", [
        ("A1", 0.96, {}),
        ("A2", 0.94, {}),
        ("A3", 0.20, {}),
    ])
    accepted = [r.external_id for r in results if r.status == "accepted"]
    assert set(accepted) == {"A1", "A2"}


def test_resolve_single_accept_mode_holds_the_rest():
    """Trials use this: one physician per named person on a trial."""
    results = resolve("x", [("N1", 0.96, {}), ("N2", 0.95, {})], multi_accept=False)
    statuses = {r.external_id: r.status for r in results}
    assert statuses["N1"] == "accepted"
    assert statuses["N2"] == "held"


def test_resolve_returns_nothing_for_hopeless_candidates():
    assert resolve("x", [("A1", 0.2, {})]) == []
    assert resolve("x", []) == []


def test_middle_initial_conflict_is_evidence_absence_is_not():
    assert middle_initial_agreement({"a"}, {"a"}) == 1.0
    assert middle_initial_agreement({"a"}, {"b"}) == -1.0
    assert middle_initial_agreement(set(), {"b"}) == 0.0
    assert middle_initial_agreement({"a"}, set()) == 0.0


def test_geo_overlap_needs_both_sides():
    assert geo_overlap({"OH", "NY"}, "OH") == 1.0
    assert geo_overlap({"OH"}, "ny") == 0.0
    assert geo_overlap(set(), "OH") == 0.0
    assert geo_overlap({"OH"}, None) == 0.0


def test_threshold_summary_counts_the_bands():
    assert summarize_thresholds([0.95, 0.75, 0.1]) == {
        "accepted": 1, "held": 1, "dropped": 1
    }


def test_name_similarity_ignores_case_punctuation_and_middle_names():
    from pex_ingest.matching import name_similarity

    assert name_similarity("Kari C. Nadeau", "kari c nadeau") == 1.0
    assert name_similarity("Gary H. Lyman", "gary herbert lyman") == 1.0
    assert name_similarity("Jon Smith", "Jonathan Smith") >= 0.9


def test_different_first_name_is_never_accepted():
    """National spot-check 2026-10-10: 'Mindie H. Nguyen' was accepted for Marielle."""
    from pex_ingest.buckets.publications.matcher import score_candidate

    physician = {"full_name": "marielle hanh nguyen", "practice_state": "CA",
                 "specialty_labels": []}
    candidate = {"display_name": "Mindie H. Nguyen", "name_variants": [],
                 "affiliations": [], "topic_ids": [], "works_count": 2}
    card = score_candidate(physician, candidate, {}, {}, namesakes=1)
    assert "first_name_mismatch" in card.signals


def test_mostly_non_medical_author_is_penalized():
    from pex_ingest.buckets.publications.matcher import medical_topic_share

    subfields = {"T1": "2208", "T2": "1708", "T3": "2740", "T4": "2312"}
    assert medical_topic_share(["T1", "T2", "T3", "T4"], subfields) == 0.25
    assert medical_topic_share([], subfields) is None


def test_mostly_foreign_recent_affiliations_block_an_accept():
    """2026-10-11: the Winnipeg IBD researcher was accepted for a Los Angeles namesake."""
    from pex_ingest.buckets.publications.matcher import recent_us_share, score_candidate

    countries = {"UCLA": "US", "WINNIPEG": "CA", "TORONTO": "CA"}
    affiliations = [
        {"inst": "WINNIPEG", "years": [2025, 2024, 2023, 2021, 2019, 2017]},
        {"inst": "TORONTO", "years": [2022, 2020]},
        {"inst": "UCLA", "years": [2018, 1996]},
    ]
    assert recent_us_share(affiliations, countries, current_year=2026) == 1 / 9
    # Old affiliations don't count; no recent ones at all is no evidence.
    assert recent_us_share([{"inst": "WINNIPEG", "years": [1990]}], countries, 2026) is None

    physician = {"full_name": "charles n bernstein", "practice_state": "CA",
                 "specialty_labels": []}
    candidate = {"display_name": "Charles N. Bernstein", "name_variants": [],
                 "affiliations": affiliations, "topic_ids": [], "works_count": 900}
    states = {"UCLA": "CA"}
    penalized = score_candidate(physician, candidate, states, {}, 5, countries)
    plain = score_candidate(physician, candidate, states, {}, 5)
    assert "mostly_foreign_recent_affiliations" in penalized.signals
    assert penalized.final() < plain.final() - 0.2


def test_us_based_author_with_a_stint_abroad_is_not_penalized():
    from pex_ingest.buckets.publications.matcher import recent_us_share

    countries = {"MGH": "US", "OXFORD": "GB"}
    affiliations = [
        {"inst": "MGH", "years": [2025, 2024, 2023, 2022, 2021]},
        {"inst": "OXFORD", "years": [2019, 2018]},
    ]
    assert recent_us_share(affiliations, countries, current_year=2026) > 0.5
