"""Branch selection and LLM reply parsing for the condition vocabulary.

Two failure modes drive these tests, and both are silent:

  * a branch that quietly stopped existing produces zero conditions, which looks
    exactly like "this branch has no conditions";
  * a batch result joined back by position instead of `custom_id` produces a
    plausible-looking condition list that is quietly wrong.
"""

from pex_ingest.buckets.conditions.branch_config import BranchConfig, BranchConfigError
from pex_ingest.buckets.conditions.enrich import build_requests, parse_reply

CONFIG = BranchConfig(
    condition_branches=["C", "F03"],
    procedure_branches=["E04"],
    excluded_branches=["F01", "F02", "F04", "D"],
)


def test_a_top_level_category_contains_its_numbered_children():
    """`C` contains `C12.294.565.500` — the category letter has no dot after it.

    Requiring a dot boundary everywhere would make every single-letter branch
    match nothing, which is the entire C/D/F configuration.
    """
    assert CONFIG.classify(["C12.294.565.500"]) == "condition"
    assert CONFIG.classify(["C"]) == "condition"


def test_deeper_prefix_matching_respects_dot_boundaries():
    """`C12.294` is under `C12`; a hypothetical `C120` is not."""
    narrow = BranchConfig(condition_branches=["C12"], procedure_branches=[],
                          excluded_branches=[])
    assert narrow.classify(["C12.294.565"]) == "condition"
    assert narrow.classify(["C120.1"]) is None


def test_mental_disorders_are_in_but_psychological_phenomena_are_out():
    """Taking all of `F` would add ~1,000 descriptors that aren't conditions."""
    assert CONFIG.classify(["F03.600.300"]) == "condition"     # Mental Disorders
    assert CONFIG.classify(["F02.463.425"]) is None            # 'Attention', 'Memory'
    assert CONFIG.classify(["F01.145"]) is None


def test_surgical_procedures_are_classified_as_procedures():
    assert CONFIG.classify(["E04.950.300"]) == "procedure"


def test_drugs_are_out_of_scope():
    assert CONFIG.classify(["D02.455.426"]) is None


def test_conditions_win_ties_with_procedures():
    """A descriptor in both trees is more useful to a patient as a condition."""
    assert CONFIG.classify(["C12.294.565", "E04.950.300"]) == "condition"


def test_any_excluded_placement_excludes_the_descriptor():
    """A descriptor sitting in both a disease branch and an excluded one is
    exactly the ambiguous case the exclusion list exists to keep out."""
    assert CONFIG.classify(["C10.228", "F01.145"]) is None


def test_a_stale_branch_fails_the_run():
    tree = [("D000001", ["C12.294.565.500"]), ("D000002", ["F03.600"])]
    try:
        CONFIG.validate_against(tree)   # E04 matches nothing in this tree
    except BranchConfigError as exc:
        assert "E04" in str(exc)
    else:
        raise AssertionError("a branch matching nothing must be a hard failure")


def test_validation_passes_when_every_branch_is_present():
    CONFIG.validate_against([
        ("D1", ["C12.294"]), ("D2", ["F03.600"]), ("D3", ["E04.950"]),
    ])


def test_descriptor_ui_is_the_batch_join_key():
    """Never positional: pairing #400's synonyms onto #12 corrupts silently."""
    requests = build_requests(
        [{"mesh_id": "D011470", "name": "Prostatic Hyperplasia", "kind": "condition"}],
        BranchConfig(
            [], [], [],
            llm={"model": "claude-sonnet-5-5", "max_tokens": 400,
                 "thinking": "between_tools", "effort": "high"},
        ),
    )
    assert requests[0]["custom_id"] == "D011470"
    assert requests[0]["params"]["model"] == "claude-sonnet-5-5"
    assert requests[0]["params"]["thinking"] == {"type": "between_tools"}
    assert requests[0]["params"]["output_config"] == {"effort": "high"}
    assert "Prostatic Hyperplasia" in requests[0]["params"]["messages"][0]["content"]


def test_parse_reply_normalizes_synonyms_to_lowercase():
    """`condition_synonym` has a CHECK that synonyms are lowercase."""
    parsed = parse_reply(
        '{"display_name": "Enlarged prostate (BPH)", "kind": "condition",'
        ' "description": "The prostate grows larger.",'
        ' "colloquial_synonyms": ["Prostate Problems", "prostate problems", "BPH"],'
        ' "body_part_synonyms": ["Prostate Doctor"]}'
    )
    assert parsed["display_name"] == "Enlarged prostate (BPH)"
    assert parsed["colloquial_synonyms"] == ["bph", "prostate problems"]   # deduped
    assert parsed["body_part_synonyms"] == ["prostate doctor"]


def test_parse_reply_tolerates_prose_around_the_json():
    parsed = parse_reply('Sure, here you go:\n{"display_name": "Gout", "kind": "condition"}')
    assert parsed["display_name"] == "Gout"
    assert parsed["colloquial_synonyms"] == []


def test_parse_reply_rejects_garbage_rather_than_guessing():
    assert parse_reply("") is None
    assert parse_reply("no json here") is None
    assert parse_reply("{not valid json") is None


def test_an_invalid_kind_is_dropped_not_passed_through():
    """`condition.kind` has a CHECK constraint; a bad value would fail the load."""
    parsed = parse_reply('{"display_name": "X", "kind": "disease"}')
    assert parsed["kind"] is None


def test_the_model_may_decline_a_descriptor():
    """A useful filter the paper count cannot provide."""
    parsed = parse_reply(
        '{"display_name": null, "kind": "condition", "colloquial_synonyms": [],'
        ' "body_part_synonyms": []}'
    )
    assert parsed["display_name"] is None
    assert parsed["colloquial_synonyms"] == []
