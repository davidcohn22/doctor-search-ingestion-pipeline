"""US state name ↔ two-letter code.

Needed because the three sources disagree: NPPES stores `practice_state` as a
two-letter code, ClinicalTrials.gov writes location states out in full ("Ohio"),
and OpenAlex institution records use a `region` field that is also usually the
full name. Geographic agreement is one of the few strong signals both matchers
have, so normalizing it is not optional.
"""

from __future__ import annotations

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
    # Territories carry NPIs and appear in both corpora.
    "puerto rico": "PR", "guam": "GU", "virgin islands": "VI",
    "american samoa": "AS", "northern mariana islands": "MP",
}

CODES = set(STATES.values())


def to_code(value: str | None) -> str | None:
    """'Ohio' → 'OH'; 'oh' → 'OH'; anything unrecognized → None.

    Returning None rather than the input is deliberate: a geo signal that fires
    on an unparseable string is worse than one that stays silent, because the
    scorer would treat garbage as agreement.
    """
    if not value:
        return None
    text = value.strip()
    if len(text) == 2 and text.upper() in CODES:
        return text.upper()
    return STATES.get(text.lower())
