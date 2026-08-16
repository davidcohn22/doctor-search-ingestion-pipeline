"""Specialty bucketing and state normalization — the two "agreement" signals.

Both matchers lean on these. Geography and specialty are the corroborating
evidence that stops a pair being accepted on name similarity alone, so a signal
that fires on garbage is worse than one that stays silent.
"""

from pex_ingest.specialties import (
    label_for_subfield,
    label_for_taxonomy,
    labels_for_taxonomies,
    specialty_fit,
)
from pex_ingest.us_states import to_code


def test_subspecialties_beat_the_generic_parent():
    """'Orthopaedic Surgery' must land in Orthopedics, not Surgery."""
    assert label_for_taxonomy("Orthopaedic Surgery") == "Orthopedics"
    assert label_for_taxonomy("Internal Medicine, Nephrology") == "Nephrology"
    assert label_for_taxonomy("Internal Medicine, Cardiovascular Disease") == "Cardiology"


def test_generic_specialties_still_resolve():
    assert label_for_taxonomy("Internal Medicine") == "Internal Medicine"
    assert label_for_taxonomy("Surgery") == "Surgery"


def test_unknown_and_empty_taxonomies():
    assert label_for_taxonomy("") == "Unknown"
    assert label_for_taxonomy(None) == "Unknown"
    # An unrecognized taxonomy keeps its own text rather than being discarded.
    assert label_for_taxonomy("Naprapath") == "Naprapath"


def test_unknown_is_dropped_from_a_physicians_label_set():
    labels = labels_for_taxonomies(["Urology", "", "Nothing Recognizable Here"])
    assert "Urology" in labels
    assert "Unknown" not in labels


def test_specialty_fit_requires_an_actual_overlap():
    assert specialty_fit(["2748"], {"Urology"}) == 1.0          # 2748 = Urology
    assert specialty_fit(["2748"], {"Cardiology"}) == 0.0
    assert specialty_fit([], {"Urology"}) == 0.0
    assert specialty_fit(["2748"], set()) == 0.0


def test_an_author_with_no_recognized_medical_subfield_scores_zero():
    """The common case for the many non-clinical authors in the pool."""
    assert specialty_fit(["9999"], {"Urology"}) == 0.0
    assert label_for_subfield("9999") is None


def test_subfield_ids_tolerate_openalex_url_form():
    assert label_for_subfield("https://openalex.org/subfields/2748") == "Urology"


def test_state_normalization_across_the_three_sources():
    assert to_code("Ohio") == "OH"        # ClinicalTrials.gov / OpenAlex region
    assert to_code("OH") == "OH"          # NPPES
    assert to_code("oh") == "OH"
    assert to_code("district of columbia") == "DC"
    assert to_code("Puerto Rico") == "PR"


def test_unrecognized_geography_returns_none_rather_than_itself():
    """A geo signal firing on garbage is worse than one that stays silent."""
    assert to_code("Ontario") is None
    assert to_code("XX") is None
    assert to_code("") is None
    assert to_code(None) is None
