"""Match physicians to OpenAlex authors — the precision-critical step.

Zero API calls and fully local: candidate retrieval is a name-key join against
the pool, and scoring is arithmetic. That means it is free to re-run, which is
what makes tuning against the eval set practical — "re-run the matcher whenever
scoring logic improves; `physician_author` upserts by `(npi, openalex_id)` so
links update in place".

**What is settled and what is not.** The structure is settled: independent
signals, an accept threshold, a held band that is never guessed at, and a margin
test so a near-tie is not resolved by a coin flip. The weights below are a
starting hypothesis and are expected to move — they should be tuned against
`eval/physician_author_pairs.csv`, optimizing precision, with recall explicitly
secondary.

**Why a physician may accept several authors.** OpenAlex routinely fragments one
human across author entities, and `physician_author`'s composite key exists to
re-unite them. So multiple accepts are allowed; what is not allowed is accepting
one candidate when an equally-scoring *rival* exists, which is the margin rule.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict

import psycopg

from ...config import Settings
from ...db import analyze, upsert
from ...matching import ORCID_CONFIRMED_SCORE, ScoreCard, best_name_similarity, resolve
from ...specialties import label_for_subfield
from . import orcid as orcid_api

log = logging.getLogger("pex.openalex.matcher")

WEIGHTS = {
    "name": 0.30,
    "geography": 0.30,
    "specialty": 0.25,
    "rarity": 0.10,
}

# Above this, an OpenAlex author entity is almost certainly a mis-grouped blob of
# many people rather than one human. Accepting one would attach thousands of
# other people's papers to a profile — the worst possible version of this
# bucket's failure mode.
MAX_WORKS = 4_000
BLOB_PENALTY = 0.25

# Candidates carrying an ORCID and scoring in this band are worth an API call:
# below it the pair is hopeless, above it already accepted.
ORCID_CHECK_BAND = (0.45, 0.95)

CANDIDATES = """
SELECT
    f.npi,
    f.full_name,
    f.practice_state,
    f.specialty_labels,
    a.openalex_author_id,
    a.display_name,
    a.name_variants,
    a.orcid,
    a.affiliations,
    a.topic_ids,
    a.works_count,
    (SELECT count(*) FROM staging.physician_name_key n WHERE n.name_key = k.name_key)
FROM staging.physician_match_facts f
JOIN staging.physician_name_key k ON k.npi = f.npi
JOIN staging.openalex_author_pool a ON a.name_key = k.name_key
WHERE a.works_count > 0
  {npi_filter}
ORDER BY f.npi
"""

UPSERT = """
INSERT INTO physician_author (npi, openalex_author_id, status, confidence, match_method,
                              orcid, orcid_confirmed, signals, matched_at)
VALUES (%(npi)s, %(author_id)s, %(status)s, %(confidence)s, %(method)s,
        %(orcid)s, %(orcid_confirmed)s, %(signals)s, now())
ON CONFLICT (npi, openalex_author_id) DO UPDATE SET
    status          = EXCLUDED.status,
    confidence      = EXCLUDED.confidence,
    match_method    = EXCLUDED.match_method,
    orcid           = EXCLUDED.orcid,
    orcid_confirmed = EXCLUDED.orcid_confirmed,
    signals         = EXCLUDED.signals,
    matched_at      = now()
-- A human 'rejected' verdict survives every re-run: 'rejected' is kept
-- specifically so refreshes don't re-propose a ruled-out candidate.
WHERE physician_author.status <> 'rejected'
"""


def author_states(affiliations, institution_states: dict) -> set[str]:
    """The set of US states an author has ever been affiliated with."""
    states = set()
    for affiliation in affiliations or []:
        state = institution_states.get(affiliation.get("inst"))
        if state:
            states.add(state)
    return states


def author_specialties(topic_ids, topic_subfields: dict) -> set[str]:
    """An author's research topics reduced to our specialty labels."""
    labels = set()
    for topic_id in topic_ids or []:
        subfield = topic_subfields.get(topic_id)
        label = label_for_subfield(subfield) if subfield else None
        if label:
            labels.add(label)
    return labels


def score_candidate(
    physician: dict,
    candidate: dict,
    institution_states: dict,
    topic_subfields: dict,
    namesakes: int,
) -> ScoreCard:
    """Score one (physician, author) pair. No I/O — ORCID is applied separately."""
    card = ScoreCard()

    names = [candidate["display_name"], *(candidate.get("name_variants") or [])]
    card.add("name", best_name_similarity(names, physician["full_name"]), WEIGHTS["name"])

    states = author_states(candidate.get("affiliations"), institution_states)
    practice_state = (physician.get("practice_state") or "").upper()
    if practice_state and practice_state in states:
        card.add("state", 1.0, WEIGHTS["geography"])

    labels = author_specialties(candidate.get("topic_ids"), topic_subfields)
    physician_labels = set(physician.get("specialty_labels") or [])
    if labels & physician_labels:
        card.add("specialty", 1.0, WEIGHTS["specialty"])

    if namesakes == 1:
        card.add("unique_name", 1.0, WEIGHTS["rarity"])
    elif namesakes <= 3:
        card.add("rare_name", 0.6, WEIGHTS["rarity"])
    elif namesakes <= 10:
        card.add("uncommon_name", 0.2, WEIGHTS["rarity"])

    if (candidate.get("works_count") or 0) > MAX_WORKS:
        card.penalize("probable_merged_entity", BLOB_PENALTY)

    return card


def run(
    conn: psycopg.Connection,
    settings: Settings,
    npis: list[str] | None = None,
    use_orcid: bool = True,
) -> dict:
    """Score every candidate pair and write `physician_author` rows."""
    from ...db import connect
    from .reference import topic_subfields, us_institution_states

    institution_states = us_institution_states(conn)
    subfields = topic_subfields(conn)
    log.info(
        "loaded %s US institutions and %s topics for scoring",
        f"{len(institution_states):,}", f"{len(subfields):,}",
    )
    if not institution_states:
        raise RuntimeError("Reference sets are empty — geography scoring would never fire.")

    orcid_client = orcid_api.OrcidChecker(conn, settings) if use_orcid else None

    npi_filter = "AND f.npi = ANY(%s)" if npis else ""
    params: tuple = (npis,) if npis else ()

    stats: defaultdict = defaultdict(int)
    rows: list[dict] = []
    current_npi = None
    physician: dict = {}
    scored: list[tuple] = []

    def flush() -> None:
        if not scored:
            return
        for decision in resolve(current_npi, scored, method="pool_seed"):
            stats[decision.status] += 1
            rows.append({
                "npi": decision.npi,
                "author_id": decision.external_id,
                "status": decision.status,
                "confidence": decision.score,
                "method": "orcid_confirmed" if decision.orcid_confirmed else decision.method,
                "orcid": decision.orcid,
                "orcid_confirmed": decision.orcid_confirmed,
                "signals": json.dumps(decision.signals),
            })

    with connect(settings) as reader, reader.cursor(name="author_pairs") as cur:
        cur.itersize = 20_000
        cur.execute(CANDIDATES.format(npi_filter=npi_filter), params)

        for row in cur:
            (npi, full_name, practice_state, specialty_labels, author_id, display_name,
             name_variants, orcid, affiliations, topic_ids, works_count, namesakes) = row

            if npi != current_npi:
                flush()
                current_npi, scored = npi, []
                physician = {
                    "full_name": full_name,
                    "practice_state": practice_state,
                    "specialty_labels": specialty_labels,
                }

            candidate = {
                "display_name": display_name,
                "name_variants": name_variants,
                "orcid": orcid,
                "affiliations": affiliations,
                "topic_ids": topic_ids,
                "works_count": works_count,
            }
            card = score_candidate(physician, candidate, institution_states, subfields, namesakes)
            stats["pairs_scored"] += 1

            # ORCID is the one signal worth an API call, and only in the band
            # where it can change the outcome. An employment record naming an
            # institution in the physician's practice state is about as close to
            # proof as this problem gets.
            if orcid_client and orcid and ORCID_CHECK_BAND[0] <= card.final() < ORCID_CHECK_BAND[1]:
                if orcid_client.confirms(orcid, practice_state):
                    card.override("orcid_confirmed", ORCID_CONFIRMED_SCORE)
                    stats["orcid_confirmations"] += 1
            if orcid:
                card.signals["_orcid"] = orcid

            scored.append((author_id, card.final(), card.signals))

    flush()

    written = upsert(conn, UPSERT, rows)
    stats["written"] = written
    analyze(conn, "physician_author")

    log.info(
        "matching: %s pairs → %s accepted, %s held (%s ORCID confirmations)",
        f"{stats['pairs_scored']:,}", f"{stats['accepted']:,}", f"{stats['held']:,}",
        f"{stats['orcid_confirmations']:,}",
    )
    if stats["accepted"] and not stats["held"]:
        log.warning(
            "the held band is empty — ambiguous cases are being silently resolved "
            "rather than parked for review"
        )
    return dict(stats)


def coverage(conn: psycopg.Connection) -> dict:
    """Match-rate baseline: report it, don't assert on it — it's a new number."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT count(*) FROM physician WHERE nppes_status = 'active'),
                count(DISTINCT npi) FILTER (WHERE status = 'accepted'),
                count(DISTINCT npi) FILTER (WHERE status = 'held'),
                count(*) FILTER (WHERE status = 'accepted')
            FROM physician_author
            """
        )
        spine, matched, held, links = cur.fetchone()
    pct = 100.0 * (matched or 0) / max(spine or 1, 1)
    log.info(
        "%s of %s active physicians have ≥1 accepted author link (%.1f%%); "
        "%s links total; %s physicians in the review queue",
        f"{matched or 0:,}", f"{spine:,}", pct, f"{links or 0:,}", f"{held or 0:,}",
    )
    return {"spine": spine, "matched": matched, "held": held, "links": links, "pct": round(pct, 2)}
