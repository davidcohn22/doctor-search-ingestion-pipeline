"""The concept vocabulary: title matching and merging per-specialty listings."""

from pex_ingest.vocabulary.merge import merge, name_keys, population_base
from pex_ingest.vocabulary.titles import TermIndex, fold, normalize, term_key


def _item(name, aliases=(), kind="condition"):
    return {"name": name, "kind": kind, "aliases": list(aliases), "everyday_names": []}


# --- title matching ----------------------------------------------------------------

def test_plurals_fold_and_hyphens_break_words():
    assert fold("pyeloplasties") == "pyeloplasty"
    assert fold("stones") == "stone"
    assert fold("stenosis") == "stenosis"
    # Folding is crude ("hynes" → "hyne") but applied identically to terms and
    # titles, so it can only merge spellings, never miss a match.
    assert normalize("Anderson-Hynes Pyeloplasties") == normalize("anderson hynes pyeloplasty")


def test_title_matching_finds_best_tier_and_name_hits():
    index = TermIndex()
    index.add(1, "UPJ obstruction", 1, True)
    index.add(1, "pyeloplasty", 1, False)
    index.add(1, "ureteral reconstruction", 2, False)
    index.add(2, "pyeloplasty", 1, True)
    hits = index.match("Robotic pyeloplasties for UPJ obstruction in infants")
    assert hits[1] == (1, True)
    assert hits[2] == (1, True)
    assert index.match("Ureteral reconstruction after injury") == {1: (2, False)}


def test_short_abbreviations_only_match_in_capitals():
    index = TermIndex()
    assert not index.add(1, "ED", 1, True)          # two letters: never
    index.add(2, "BPH", 1, True)                    # three letters: capitals only
    index.add(3, "MESA", 1, True, abbreviation=True)
    index.add(4, "gout", 1, True)                   # an ordinary short word
    assert index.match("Outcomes of BPH surgery") == {2: (1, True)}
    assert index.match("bph in a lowercase title") == {}
    assert index.match("Life on the mesa") == {}
    assert 3 in index.match("Sperm retrieval by MESA")
    assert 4 in index.match("Gout flares")
    assert term_key("MESA", abbreviation=True) == "MESA"


# --- merging -------------------------------------------------------------------------

def test_same_name_merges_across_groups_and_ignores_a_trailing_abbreviation():
    concepts = merge({
        "urology": [_item("Overactive bladder (OAB)", ["OAB"])],
        "urogynecology": [_item("Overactive bladder")],
    })
    assert len(concepts) == 1
    assert concepts[0].groups == ["urology", "urogynecology"]


def test_one_sided_alias_never_merges_two_concepts():
    """Full run 2026-10-11: adrenal tumors and glomus tumors were chained into
    pheochromocytoma through aliases that were really related terms."""
    concepts = merge({
        "endocrinology": [_item("Pheochromocytoma", ["paraganglioma", "adrenal tumor"])],
        "urology": [_item("Adrenal tumor", ["adrenal mass"])],
    })
    names = sorted(c.name for c in concepts)
    assert names == ["Adrenal tumor", "Pheochromocytoma"]
    pheo = next(c for c in concepts if c.name == "Pheochromocytoma")
    # And the other concept's name is not kept as an alias.
    assert "adrenal tumor" not in [a.lower() for a in pheo.aliases]


def test_mutual_aliases_merge():
    concepts = merge({
        "urology": [_item("Kidney stones", ["nephrolithiasis"])],
        "nephrology": [_item("Nephrolithiasis", ["kidney stones"])],
    })
    assert len(concepts) == 1


def test_a_broader_one_word_alias_never_swallows_a_concept():
    concepts = merge({
        "ophthalmology": [_item("Glaucoma")],
        "pediatric-ophthalmology": [_item("Congenital glaucoma", ["glaucoma", "buphthalmos"])],
    })
    assert sorted(c.name for c in concepts) == ["Congenital glaucoma", "Glaucoma"]
    congenital = next(c for c in concepts if c.name == "Congenital glaucoma")
    assert congenital.aliases == ["buphthalmos"]


def test_population_variants_fold_into_the_base_concept_one_way():
    assert population_base("Priapism in children") == "priapism"
    assert population_base("Juvenile idiopathic arthritis") is None
    assert name_keys("Overactive bladder (OAB)") == {"overactive bladder oab", "overactive bladder"}
    concepts = merge({
        "urology": [_item("Priapism")],
        "pediatric-urology": [_item("Priapism in children")],
    })
    assert len(concepts) == 1
    assert concepts[0].name == "Priapism"
    assert set(concepts[0].groups) == {"urology", "pediatric-urology"}
