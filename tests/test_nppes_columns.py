"""NPPES header resolution.

The file has ~330 columns and the V1/V2 layouts spell them differently. A silent
mis-parse produces a plausible-looking, wrong database — much more expensive than
a job that refuses to start — so the resolver's job is to fail loudly.
"""

from pex_ingest.buckets.nppes.columns import (
    REQUIRED,
    TAXONOMY_SLOTS,
    normalize_header,
    projection_sql,
    resolve,
    taxonomy_filter_sql,
)

# The V1 spellings, with their parenthetical qualifiers.
V1 = [
    "NPI", "Entity Type Code",
    "Provider Last Name (Legal Name)", "Provider First Name", "Provider Middle Name",
    "Provider Name Suffix Text", "Provider Credential Text", "Provider Gender Code",
    "Provider First Line Business Practice Location Address",
    "Provider Business Practice Location Address City Name",
    "Provider Business Practice Location Address State Name",
    "Provider Business Practice Location Address Postal Code",
    "Provider Enumeration Date", "Last Update Date",
    "NPI Deactivation Date", "NPI Reactivation Date", "Is Sole Proprietor",
] + [
    f"Healthcare Provider Taxonomy Code_{n}" for n in range(1, TAXONOMY_SLOTS + 1)
] + [
    f"Provider License Number State Code_{n}" for n in range(1, TAXONOMY_SLOTS + 1)
]

# The same columns without the parentheticals — the V2 shape.
V2 = [h.replace(" (Legal Name)", "") for h in V1]


def test_normalization_collapses_the_two_layouts_to_one_name():
    assert normalize_header("Provider Last Name (Legal Name)") == "provider_last_name"
    assert normalize_header("Provider Last Name") == "provider_last_name"
    assert (
        normalize_header("Provider Business Mailing Address Country Code (If outside U.S.)")
        == "provider_business_mailing_address_country_code"
    )


def test_both_layouts_resolve_identically():
    v1, v2 = resolve(V1), resolve(V2)
    assert v1.keys() == v2.keys()
    assert v1["last_name"] == "Provider Last Name (Legal Name)"
    assert v2["last_name"] == "Provider Last Name"
    for canonical in REQUIRED:
        assert canonical in v1 and canonical in v2


def test_all_fifteen_taxonomy_slots_resolve():
    """Only reading slot 1 collapses the spine from ~1M rows to ~200K."""
    resolved = resolve(V1)
    for slot in range(1, TAXONOMY_SLOTS + 1):
        assert f"taxonomy_code_{slot}" in resolved


def test_missing_required_columns_fail_loudly():
    try:
        resolve(["NPI", "Some Other Column"])
    except RuntimeError as exc:
        assert "missing required columns" in str(exc)
        assert "last_name" in str(exc)
    else:
        raise AssertionError("a layout change must fail the run, not pass silently")


def test_the_physician_filter_covers_every_slot():
    clause = taxonomy_filter_sql(resolve(V1))
    assert clause.count("LIKE '20%'") == TAXONOMY_SLOTS
    assert clause.startswith("(") and " OR " in clause


def test_projection_aliases_actual_headers_to_stable_names():
    sql = projection_sql(resolve(V1))
    assert '"Provider Last Name (Legal Name)" AS last_name' in sql
    assert '"Entity Type Code" AS entity_type_code' in sql
