"""Name normalization — the code both matchers depend on.

This is the most-tested module in the pipeline on purpose. It runs over a million
times per matcher pass, both buckets share it, and every failure mode is silent:
a key that normalizes wrongly doesn't crash, it just quietly stops finding a
physician (a false negative) or starts finding the wrong one (a false positive,
which is the expensive kind).
"""

from pex_ingest.names import (
    clean_token,
    initials_of,
    name_key,
    name_key_parts,
    name_keys_for_physician,
    parse_name,
    strip_accents,
    tokenize,
)


def test_the_three_source_shapes_produce_one_key():
    """NPPES, ClinicalTrials.gov and OpenAlex spell the same human differently."""
    assert name_key_parts("SARFARAZ", "HASNI") == "hasni|s"
    assert name_key("Sarfaraz A Hasni, M.D.") == "hasni|s"
    assert name_key("Sarfaraz A. Hasni") == "hasni|s"


def test_credentials_are_stripped():
    for raw in [
        "Jane Roe, M.D.",
        "Jane Roe MD",
        "Jane Roe, M.D., Ph.D.",
        "Jane Roe, MD, FACS",
        "Dr. Jane Roe",
    ]:
        assert name_key(raw) == "roe|j", raw


def test_generational_suffixes_do_not_become_the_surname():
    assert name_key("John Smith Jr.") == "smith|j"
    assert name_key("John Smith III") == "smith|j"
    assert name_key_parts("John", "Smith Jr") == "smith|j"


def test_comma_inversion():
    assert name_key("Hasni, Sarfaraz A") == "hasni|s"
    assert name_key("Roe, Jane") == "roe|j"


def test_comma_before_credentials_is_not_an_inversion():
    """'Hasni, M.D.' must not read as given name 'M D', surname 'Hasni'."""
    assert name_key("Sarfaraz Hasni, M.D.") == "hasni|s"


def test_accents_fold():
    """Sources disagree about diacritics for the same person."""
    assert strip_accents("Muñoz") == "Munoz"
    assert name_key("José Muñoz") == "munoz|j"
    assert name_key("Jose Munoz") == "munoz|j"


def test_surname_particles_stay_with_the_surname():
    """'van der Berg' keys on the whole surname, not on 'berg'."""
    assert name_key("Jane van der Berg") == "vanderberg|j"
    assert name_key("Piet de Vries") == "devries|p"


def test_middle_initials_reduce_to_a_letter_set():
    first, middles, last = parse_name("Gregory Y.H. Lip")
    assert (first, last) == ("gregory", "lip")
    assert middles == {"y", "h"}

    _first, middles_spelled, _last = parse_name("Gregory Yee Hoong Lip")
    assert middles_spelled == {"y", "h"}


def test_unusable_names_return_none_rather_than_a_junk_key():
    """None means 'not a candidate for anything' — the partial index relies on it."""
    assert name_key("") is None
    assert name_key("Smith") is None            # surname only
    assert name_key("J S") is None              # initials only
    assert name_key("M.D.") is None
    assert name_key_parts("J", "Smith") is None  # single-letter given name


def test_alternate_names_are_all_indexed():
    """A surgeon who published under a maiden name must still be findable."""
    keys = name_keys_for_physician(
        "Jane", "Roe", [{"first_name": "Jane", "last_name": "Doe"}]
    )
    assert keys == {"roe|j", "doe|j"}


def test_alternate_names_accept_plain_strings_too():
    keys = name_keys_for_physician("Jane", "Roe", ["Jane Q. Doe"])
    assert "doe|j" in keys and "roe|j" in keys


def test_alternate_name_without_a_first_name_inherits_the_legal_one():
    keys = name_keys_for_physician("Jane", "Roe", [{"last_name": "Doe"}])
    assert keys == {"roe|j", "doe|j"}


def test_hyphenated_surnames_are_one_token():
    assert name_key("Jane Roe-Smith") == "roesmith|j"


def test_tokenize_drops_punctuation_and_honorifics():
    assert tokenize("Prof. Jane Q. Roe, M.D.") == ["jane", "q", "roe"]


def test_clean_token_and_initials():
    assert clean_token("O'Brien") == "obrien"
    assert initials_of("Ann Marie") == {"a", "m"}
    assert initials_of("") == set()
