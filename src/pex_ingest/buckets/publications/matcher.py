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
from ...matching import (
    ORCID_CONFIRMED_SCORE,
    ScoreCard,
    best_name_similarity,
    middle_initial_agreement,
    resolve,
)
from ...names import parse_name
from ...specialties import specialty_fit
from .orcid_check import BLOCKING_SIGNALS, verdict

log = logging.getLogger("pex.openalex.matcher")

# Rebalanced 2026-10-10 against eval/physician_author_pairs.csv (first live run).
# The original weights summed to 0.95, so a pair needed a perfect name, state,
# specialty AND a rare name to reach 0.90: 11 of 197 seed pairs were accepted.
# Now: complete evidence on a unique or rare name clears the bar; 4–10 namesakes
# also need a matching middle initial; >10 namesakes never auto-accept without
# ORCID. One author accepted for two physicians is demoted (see run()).
WEIGHTS = {
    "name": 0.30,
    "geography": 0.25,
    "specialty": 0.20,
    "rarity": 0.15,
    "middle": 0.10,
}
MIDDLE_PENALTY = 0.15
# Below this name score the first names genuinely differ (prefixes like Jon /
# Jonathan score 0.96, Jon / John ~0.94); the penalty keeps such pairs held.
NAME_FLOOR = 0.90
FIRST_NAME_PENALTY = 0.15

# Above this, an OpenAlex author entity is almost certainly a mis-grouped blob of
# many people rather than one human. Accepting one would attach thousands of
# other people's papers to a profile — the worst possible version of this
# bucket's failure mode.
MAX_WORKS = 4_000
BLOB_PENALTY = 0.25

# Candidates carrying an ORCID and scoring at least this much can be confirmed by
# their ORCID record; below it the pair is hopeless whatever ORCID says.
ORCID_CHECK_BAND = (0.45, 0.95)
# An ORCID record listing only non-US affiliations belongs to someone abroad.
ORCID_FOREIGN_PENALTY = 0.30
# An ORCID whose owner has a different first name belongs to someone else.
ORCID_NAME_PENALTY = 0.15

# Rarity counts physicians sharing the full first + last name. It used to count
# the lookup key (surname + first initial), which made "Emery Brown" as common as
# every "E. Brown" and held clear matches below the accept line.
CANDIDATES = """
WITH full_name_counts AS (
    SELECT lower(first_name) AS first_l, lower(last_name) AS last_l, count(*) AS n
    FROM staging.physician_match_facts
    GROUP BY 1, 2
)
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
    coalesce(fc.n, 1)
FROM staging.physician_match_facts f
LEFT JOIN full_name_counts fc
       ON fc.first_l = lower(f.first_name) AND fc.last_l = lower(f.last_name)
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


# Foreign namesakes (2026-10-11). The state signal gives full credit for *ever*
# having an affiliation in the physician's state, so one US visit outweighed a
# decade abroad: the #1 IBD result was a Los Angeles doctor credited with 452
# papers by the Winnipeg IBD researcher of the same name. A profile whose recent
# affiliations are mostly outside the US is evidence of a different person.
RECENT_YEARS = 10
MIN_US_SHARE = 0.5
FOREIGN_PENALTY = 0.25


def recent_us_share(
    affiliations, institution_countries: dict, current_year: int | None = None
) -> float | None:
    """Share of an author's recent affiliation-years at US institutions.

    Each (institution, year) pair in the last RECENT_YEARS counts once, so a
    decade at one Canadian hospital outweighs a single year's US co-affiliation.
    None when the author has no recent affiliation with a known country, which
    is no evidence either way.
    """
    import datetime

    year_now = current_year or datetime.date.today().year
    us = total = 0
    for affiliation in affiliations or []:
        country = institution_countries.get(affiliation.get("inst"))
        if not country:
            continue
        recent = sum(1 for y in affiliation.get("years") or [] if y and y > year_now - RECENT_YEARS)
        total += recent
        if country == "US":
            us += recent
    if total == 0:
        return None
    return us / total


def author_subfields(topic_ids, topic_subfields: dict) -> list[str]:
    """An author's research topics reduced to OpenAlex subfield ids."""
    return [topic_subfields[t] for t in topic_ids or [] if topic_subfields.get(t)]


# OpenAlex fields (subfield id // 100) that a practising physician's research
# plausibly sits in: Biochemistry/Genetics 13, Immunology/Microbiology 24,
# Medicine 27, Neuroscience 28, Nursing 29, Pharmacology 30, Psychology 32,
# Dentistry 35, Health Professions 36.
MEDICAL_FIELDS = {13, 24, 27, 28, 29, 30, 32, 35, 36}
MIN_MEDICAL_SHARE = 0.5
NON_MEDICAL_PENALTY = 0.15


def medical_topic_share(topic_ids, topic_subfields: dict) -> float | None:
    """Share of an author's topics in medical/life-science fields; None if unknown."""
    fields = []
    for topic_id in topic_ids or []:
        subfield = topic_subfields.get(topic_id)
        if subfield and str(subfield).isdigit():
            fields.append(int(subfield) // 100)
    if not fields:
        return None
    return sum(f in MEDICAL_FIELDS for f in fields) / len(fields)


def score_candidate(
    physician: dict,
    candidate: dict,
    institution_states: dict,
    topic_subfields: dict,
    namesakes: int,
    institution_countries: dict | None = None,
) -> ScoreCard:
    """Score one (physician, author) pair. No I/O — ORCID is applied separately."""
    card = ScoreCard()

    names = [candidate["display_name"], *(candidate.get("name_variants") or [])]
    name_score = best_name_similarity(names, physician["full_name"])
    card.add("name", name_score, WEIGHTS["name"])
    if name_score < NAME_FLOOR:
        # Same surname, different first name ("Marielle"/"Mindie" Nguyen, "Anders"/
        # "Ari" Cohen): corroborating state + specialty + middle initial were enough
        # to accept these in the first national spot-check. Never auto-accept them.
        card.penalize("first_name_mismatch", FIRST_NAME_PENALTY)

    # Middle initials: agreement is evidence for, a conflict is evidence against,
    # absence on either side is neither. Any spelling carrying a middle counts.
    _first, physician_middles, _last = parse_name(physician["full_name"])
    author_middles: set = set()
    for name in names:
        if name:
            author_middles |= parse_name(name)[1]
    agreement = middle_initial_agreement(physician_middles, author_middles)
    if agreement > 0:
        card.add("middle_initial", 1.0, WEIGHTS["middle"])
    elif agreement < 0:
        card.penalize("middle_conflict", MIDDLE_PENALTY)

    states = author_states(candidate.get("affiliations"), institution_states)
    practice_state = (physician.get("practice_state") or "").upper()
    if practice_state and practice_state in states:
        card.add("state", 1.0, WEIGHTS["geography"])

    fit = specialty_fit(
        author_subfields(candidate.get("topic_ids"), topic_subfields),
        physician.get("specialty_labels"),
    )
    if fit:
        card.add("specialty", fit, WEIGHTS["specialty"])

    if namesakes == 1:
        card.add("unique_name", 1.0, WEIGHTS["rarity"])
    elif namesakes <= 3:
        card.add("rare_name", 0.6, WEIGHTS["rarity"])
    elif namesakes <= 10:
        card.add("uncommon_name", 0.4, WEIGHTS["rarity"])

    # An engineer or a merged blob sharing the physician's name can pick up one
    # stray medical topic and one US affiliation ("Kitae Park": chip design,
    # orthodontics and a Twitter office in CA, accepted for a Compton intensivist
    # in the first national spot-check). Mostly non-medical research is evidence
    # of a different person.
    share = medical_topic_share(candidate.get("topic_ids"), topic_subfields)
    if share is not None and share < MIN_MEDICAL_SHARE:
        card.penalize("non_medical_topics", NON_MEDICAL_PENALTY)

    if institution_countries:
        us_share = recent_us_share(candidate.get("affiliations"), institution_countries)
        if us_share is not None and us_share < MIN_US_SHARE:
            card.penalize("mostly_foreign_recent_affiliations", FOREIGN_PENALTY)

    if (candidate.get("works_count") or 0) > MAX_WORKS:
        card.penalize("probable_merged_entity", BLOB_PENALTY)

    return card


def load_orcid_affiliations(conn: psycopg.Connection) -> dict[str, tuple[list, str | None]]:
    """{orcid: (affiliations, owner's given names)} from the ORCID check's records."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('staging.orcid_record')")
        if cur.fetchone()[0] is None:
            return {}
        cur.execute("SELECT orcid, affiliations, given_names FROM staging.orcid_record "
                    "WHERE http_status = 200")
        return {orcid: (affiliations, given) for orcid, affiliations, given in cur.fetchall()}


def demote_shared_authors(rows: list[dict], stats) -> int:
    """An author accepted for 2+ physicians keeps only a clear winner.

    The best-scoring physician keeps the link when it beats the runner-up by at
    least MIN_MARGIN; otherwise nothing separates them and every claim is held.
    """
    from ...matching import MIN_MARGIN

    claims: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["status"] == "accepted":
            claims[row["author_id"]].append(row)
    demoted = 0
    for claimants in claims.values():
        if len(claimants) < 2:
            continue
        claimants.sort(key=lambda r: r["confidence"], reverse=True)
        clear = claimants[0]["confidence"] - claimants[1]["confidence"] >= MIN_MARGIN
        for row in claimants[1:] if clear else claimants:
            row["status"] = "held"
            demoted += 1
    stats["accepted"] -= demoted
    stats["held"] += demoted
    stats["demoted_shared_author"] = demoted
    return demoted


DEMOTE_SHARED_AUTHORS = """
WITH claims AS (
    SELECT npi, openalex_author_id, confidence,
           row_number() OVER w AS rank,
           count(*) OVER (PARTITION BY openalex_author_id) AS claimants,
           first_value(confidence) OVER w
               - nth_value(confidence, 2) OVER (w ROWS BETWEEN UNBOUNDED PRECEDING
                                                AND UNBOUNDED FOLLOWING) AS winner_margin
    FROM physician_author
    WHERE status = 'accepted'
    WINDOW w AS (PARTITION BY openalex_author_id ORDER BY confidence DESC, npi)
)
UPDATE physician_author pa
SET status = 'held',
    signals = coalesce(pa.signals, '{}'::jsonb) || '{"shared_author_demoted": true}'::jsonb
FROM claims c
WHERE pa.npi = c.npi AND pa.openalex_author_id = c.openalex_author_id
  AND c.claimants > 1
  -- A clear winner (ahead by MIN_MARGIN) keeps the author; everyone else, or
  -- everyone when nothing separates them, is held.
  AND NOT (c.rank = 1 AND c.winner_margin >= %(margin)s)
"""


def demote_shared_authors_in_db(conn: psycopg.Connection) -> int:
    """The same rule as demote_shared_authors, across every worker's output."""
    from ...matching import MIN_MARGIN

    with conn.cursor() as cur:
        cur.execute(DEMOTE_SHARED_AUTHORS, {"margin": MIN_MARGIN})
        demoted = cur.rowcount
    conn.commit()
    log.info("demoted %s accepted rows whose author is claimed by 2+ physicians",
             f"{demoted:,}")
    return demoted


def run(
    conn: psycopg.Connection,
    settings: Settings,
    npis: list[str] | None = None,
    use_orcid: bool = True,
    partition: tuple[int, int] | None = None,
) -> dict:
    """Score every candidate pair and write `physician_author` rows."""
    from ...db import connect
    from .reference import institution_countries, topic_subfields, us_institution_states

    institution_states = us_institution_states(conn)
    countries = institution_countries(conn)
    subfields = topic_subfields(conn)
    log.info(
        "loaded %s US institutions and %s topics for scoring",
        f"{len(institution_states):,}", f"{len(subfields):,}",
    )
    if not institution_states:
        raise RuntimeError("Reference sets are empty — geography scoring would never fire.")

    # ORCID evidence is read from records the ORCID check job already fetched
    # (orcid_check.py), never requested here: inline calls at a polite pace made
    # a national run take days, so the first run skipped ORCID entirely.
    orcid_records = load_orcid_affiliations(conn) if use_orcid else {}

    npi_filter = "AND f.npi = ANY(%s)" if npis else ""
    params: tuple = (npis,) if npis else ()
    if partition:
        # Worker k of n takes the NPIs whose hash falls in its slot; together the
        # n workers cover every physician exactly once.
        npi_filter += " AND abs(hashtext(f.npi)) %% %s = %s"
        params = (*params, partition[1], partition[0])

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
            card = score_candidate(
                physician, candidate, institution_states, subfields, namesakes, countries
            )
            stats["pairs_scored"] += 1
            if stats["pairs_scored"] % 5_000_000 == 0:
                # ~300M pairs nationally; without this the run is silent for hours.
                log.info(
                    "  … %s pairs scored (%s accepted, %s held so far)",
                    f"{stats['pairs_scored']:,}", f"{stats['accepted']:,}", f"{stats['held']:,}",
                )

            # An ORCID record naming an organization in the physician's practice
            # state is about as close to proof as this problem gets; one listing
            # only foreign affiliations is strong evidence of a namesake abroad.
            if orcid and orcid in orcid_records:
                affiliations, given = orcid_records[orcid]
                result = verdict(affiliations, practice_state, given,
                                 parse_name(full_name)[0])
                blocked = any(card.signals.get(s) for s in BLOCKING_SIGNALS)
                if (result == "confirmed" and not blocked
                        and ORCID_CHECK_BAND[0] <= card.final() < ORCID_CHECK_BAND[1]):
                    card.override("orcid_confirmed", ORCID_CONFIRMED_SCORE)
                    stats["orcid_confirmations"] += 1
                elif result == "foreign":
                    card.penalize("orcid_foreign", ORCID_FOREIGN_PENALTY)
                    stats["orcid_foreign"] += 1
                elif result == "name_mismatch":
                    card.penalize("orcid_name_mismatch", ORCID_NAME_PENALTY)
                    stats["orcid_name_mismatch"] += 1
            if orcid:
                card.signals["_orcid"] = orcid

            scored.append((author_id, card.final(), card.signals))

    flush()

    # One OpenAlex author is one human. Accepted for two different physicians, at
    # least one is wrong: keep a clear winner, otherwise demote them all.
    demote_shared_authors(rows, stats)

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
