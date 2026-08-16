"""Match trial person mentions to the physician spine.

Same three-stage shape as the publications matcher, and deliberately the same
`names` module and the same thresholds — the two buckets share one definition of
"same name" rather than drifting apart.

The scoring is a *starting point*, not a tuned model. Every weight below is a
hypothesis to be measured against the eval set; the structure (independent
signals, accept/hold/drop, never accept on name alone) is the part that is
settled. "Never accept on name alone. Common surnames over-match badly, and the
same asymmetry from bucket 3 applies: a missing trial is a small gap, a wrong
trial on a profile is a credibility failure."
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict

import psycopg

from ...db import upsert
from ...matching import (
    ScoreCard,
    best_name_similarity,
    middle_initial_agreement,
    resolve,
)
from ...names import initials_of, parse_name
from ...us_states import to_code

log = logging.getLogger("pex.trials.matcher")

# Roles that indicate a clinical investigator rather than an administrator.
INVESTIGATOR_ROLES = {
    "PRINCIPAL_INVESTIGATOR", "SUB_INVESTIGATOR", "STUDY_CHAIR", "STUDY_DIRECTOR",
}

WEIGHTS = {
    "name": 0.35,
    "geography": 0.30,
    "rarity": 0.20,
    "role": 0.08,
    "credential": 0.07,
}

MIDDLE_PENALTY = 0.15

# Candidate pairs, joined through the shared name-key index and ordered by
# mention: the competing candidates for "who is this named person?" are the
# physicians sharing that name, so they have to be scored and compared together.
CANDIDATES = """
SELECT
    m.mention_id,
    m.nct_id,
    m.name,
    m.source_field,
    m.role,
    m.affiliation,
    m.facility,
    m.city,
    m.state,
    f.npi,
    f.first_name,
    f.middle_name,
    f.last_name,
    f.full_name,
    f.credential,
    f.practice_city,
    f.practice_state,
    f.specialty_labels
FROM staging.trial_person_index m
JOIN staging.physician_name_key k ON k.name_key = m.name_key
JOIN staging.physician_match_facts f ON f.npi = k.npi
WHERE m.name_key IS NOT NULL
  {trial_filter}
ORDER BY m.mention_id
"""

UPSERT = """
INSERT INTO trial_investigator (npi, nct_id, role, source_field, site_facility,
                                site_city, site_state, confidence, status, signals,
                                matched_at)
VALUES (%(npi)s, %(nct_id)s, %(role)s, %(source_field)s, %(facility)s,
        %(city)s, %(state)s, %(confidence)s, %(status)s, %(signals)s, now())
ON CONFLICT (npi, nct_id) DO UPDATE SET
    role          = EXCLUDED.role,
    source_field  = EXCLUDED.source_field,
    site_facility = EXCLUDED.site_facility,
    site_city     = EXCLUDED.site_city,
    site_state    = EXCLUDED.site_state,
    confidence    = EXCLUDED.confidence,
    status        = EXCLUDED.status,
    signals       = EXCLUDED.signals,
    matched_at    = now()
-- A human 'rejected' verdict is never overwritten by a re-run: that decision is
-- the whole reason the status exists.
WHERE trial_investigator.status <> 'rejected'
"""


def score_mention(mention: dict, physician: dict, namesakes: int) -> ScoreCard:
    """Score one (trial mention, physician) pair.

    Signals are independent by design, so no single weak source can carry a pair
    over the line on its own — the same principle the eval-set bootstrapper uses.
    """
    card = ScoreCard()

    card.add("name", best_name_similarity([mention["name"]], physician["full_name"]),
             WEIGHTS["name"])

    # Geography. A site contact carries its site's city/state, which is stronger
    # evidence than an overall official's free-text affiliation — so the site
    # state is checked first and the affiliation string only as a fallback.
    site_state = to_code(mention.get("state"))
    practice_state = to_code(physician.get("practice_state"))
    if site_state and practice_state and site_state == practice_state:
        card.add("state", 1.0, WEIGHTS["geography"])
        city = (mention.get("city") or "").strip().lower()
        practice_city = (physician.get("practice_city") or "").strip().lower()
        if city and city == practice_city:
            card.add("city", 1.0, 0.05)
    elif mention.get("affiliation") and practice_state:
        # Free-text affiliation: look for the physician's city by name, since a
        # two-letter state code inside prose matches far too loosely.
        practice_city = (physician.get("practice_city") or "").strip().lower()
        if practice_city and practice_city in mention["affiliation"].lower():
            card.add("affiliation_city", 1.0, WEIGHTS["geography"] * 0.7)

    # Name rarity, measured in the registry rather than assumed.
    if namesakes == 1:
        card.add("unique_name", 1.0, WEIGHTS["rarity"])
    elif namesakes <= 3:
        card.add("rare_name", 0.6, WEIGHTS["rarity"])
    elif namesakes <= 10:
        card.add("uncommon_name", 0.2, WEIGHTS["rarity"])
    # More than ten namesakes contributes nothing — which is the honest answer.

    if (mention.get("role") or "").upper() in INVESTIGATOR_ROLES:
        card.add("investigator_role", 1.0, WEIGHTS["role"])

    credential = (physician.get("credential") or "").upper().replace(".", "")
    if "MD" in credential or "DO" in credential:
        card.add("md_do", 1.0, WEIGHTS["credential"])

    # Middle initials: agreement helps a little, conflict is real evidence of two
    # different people.
    _first, mention_initials, _last = parse_name(mention["name"])
    agreement = middle_initial_agreement(
        mention_initials, initials_of(physician.get("middle_name") or "")
    )
    if agreement > 0:
        card.add("middle_initial", 1.0, 0.05)
    elif agreement < 0:
        card.penalize("middle_conflict", MIDDLE_PENALTY)

    return card


def run(conn: psycopg.Connection, settings, nct_ids: list[str] | None = None) -> dict:
    """Score every candidate pair and write `trial_investigator` rows.

    `nct_ids` restricts the pass to specific trials, which is what the weekly
    delta uses: a changed trial re-matches only its own name keys instead of
    re-running the whole corpus.
    """
    from ...db import connect
    from ...spine import namesake_counts

    counts = namesake_counts(conn)
    log.info("loaded namesake counts for %s name keys", f"{len(counts):,}")

    trial_filter = ""
    params: tuple = ()
    if nct_ids:
        trial_filter = "AND m.nct_id = ANY(%s)"
        params = (nct_ids,)

    stats: defaultdict = defaultdict(int)
    # Keyed (npi, nct_id) — the primary key of trial_investigator. A physician
    # named twice on one trial (overall official *and* site contact) collapses to
    # the better-scoring mention.
    best_rows: dict[tuple, dict] = {}

    current_mention = None
    mention_info: dict = {}
    scored: list[tuple] = []

    def flush() -> None:
        """Decide who a single named person on a single trial actually is.

        Everything competing here shares a name key, so this is precisely the
        "which of the same-named doctors is it?" question — and the margin rule
        does the work: a clear winner is accepted, a near-tie is held for review
        rather than guessed at.
        """
        if not scored:
            return
        # `resolve` is used inverted here: the "candidates" are NPIs competing
        # for one mention, so the external id it ranks *is* the NPI. Same margin
        # and threshold rules either way, which is the point of sharing it.
        for decision in resolve(
            npi="", scored=[(npi, score, signals) for npi, score, signals in scored],
            method="pool_seed", multi_accept=False,
        ):
            npi = decision.external_id
            stats[decision.status] += 1
            key = (npi, mention_info["nct_id"])
            existing = best_rows.get(key)
            if existing and existing["confidence"] >= decision.score:
                continue
            best_rows[key] = {
                "npi": npi,
                "nct_id": mention_info["nct_id"],
                "role": mention_info.get("role"),
                "source_field": mention_info["source_field"],
                "facility": mention_info.get("facility"),
                "city": mention_info.get("city"),
                "state": mention_info.get("state"),
                "confidence": decision.score,
                "status": decision.status,
                "signals": json.dumps(decision.signals),
            }

    with connect(settings) as reader, reader.cursor(name="trial_pairs") as cur:
        cur.itersize = 20_000
        cur.execute(CANDIDATES.format(trial_filter=trial_filter), params)
        for row in cur:
            (mention_id, nct_id, name, source_field, role, affiliation, facility,
             city, state, npi, first, middle, last, full, credential,
             p_city, p_state, labels) = row

            if mention_id != current_mention:
                flush()
                current_mention = mention_id
                scored = []
                mention_info = {
                    "nct_id": nct_id, "name": name, "source_field": source_field,
                    "role": role, "affiliation": affiliation, "facility": facility,
                    "city": city, "state": state,
                }

            physician = {
                "first_name": first, "middle_name": middle, "last_name": last,
                "full_name": full, "credential": credential,
                "practice_city": p_city, "practice_state": p_state,
                "specialty_labels": labels,
            }
            key = f"{(last or '').lower()}|{(first or '')[:1].lower()}"
            card = score_mention(mention_info, physician, counts.get(key, 99))
            stats["pairs_scored"] += 1
            scored.append((npi, card.final(), card.signals))

    flush()

    written = upsert(conn, UPSERT, list(best_rows.values()))
    stats["written"] = written
    log.info(
        "trial matching: %s pairs scored → %s accepted, %s held",
        f"{stats['pairs_scored']:,}", f"{stats['accepted']:,}", f"{stats['held']:,}",
    )
    if stats["accepted"] and not stats["held"]:
        log.warning(
            "no held matches — the threshold logic is not splitting ambiguous "
            "cases, which means something is being silently guessed"
        )
    return dict(stats)
