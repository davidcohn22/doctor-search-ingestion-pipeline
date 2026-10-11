"""Shared matching machinery for buckets 3 (publications) and 4 (trials).

Both buckets face the same problem: no publication or trial database contains an
NPI, so linking a physician to an author or an investigator is *probabilistic* —
a scoring decision, not a lookup. Both therefore use the same three-stage shape:

    1. candidate retrieval   — broad and dumb, by `names.name_key`
    2. scoring               — several independent signals, weighted
    3. thresholding          — accept / hold / drop

The asymmetry that drives every threshold in this file:

    "A missing paper on a profile is a small gap a user will never notice.
     A wrong paper on a profile is the most credibility-destroying bug this
     product can ship."  — docs/prerequisites/03-publications.md

So precision is the metric to optimize and recall is explicitly secondary, and
anything genuinely ambiguous is *held*, never guessed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("pex.matching")

# Thresholds, per the publications runbook's matcher section.
ACCEPT = 0.90   # → physician_author / trial_investigator with status 'accepted'
HOLD = 0.60     # 0.60–0.90 → 'held': parked for review, never displayed
# Below HOLD the pair is dropped entirely rather than stored as 'rejected'.
# 'rejected' is reserved for a human ruling a candidate out, so that a later
# refresh doesn't re-propose it — storing every weak scorer output there would
# bury those decisions under millions of rows nobody chose.

# An ORCID employment record that agrees on institution is near-certainty: it is
# the one signal that ties an author identity to a workplace the physician's own
# NPPES record also names.
ORCID_CONFIRMED_SCORE = 0.97

# A best candidate this close to its runner-up is ambiguous by definition — two
# same-named doctors in the same specialty, and no evidence separating them.
MIN_MARGIN = 0.08


@dataclass
class ScoreCard:
    """Accumulates weighted signals into a score plus its explainability trail.

    `signals` is written to the `signals` jsonb column on physician_author and
    trial_investigator, so every accepted match can be explained after the fact —
    which is both a debugging tool and, given the product's evidence-first
    philosophy, the honest way to store a probabilistic conclusion.
    """

    score: float = 0.0
    signals: dict = field(default_factory=dict)

    def add(self, name: str, value: float, weight: float) -> None:
        """Record a signal in 0–1 and fold its weighted contribution in."""
        if value <= 0:
            return
        value = min(1.0, max(0.0, value))
        self.score += weight * value
        self.signals[name] = round(value, 3)

    def penalize(self, name: str, amount: float) -> None:
        """Subtract for positive evidence *against* a pair.

        Distinct from a signal that simply didn't fire: a middle-initial conflict
        is evidence of two different people, whereas a missing middle initial is
        no evidence at all.
        """
        self.score -= amount
        self.signals[name] = -round(amount, 3)

    def override(self, name: str, floor: float) -> None:
        """Raise the score to a floor — used only by ORCID confirmation."""
        if self.score < floor:
            self.score = floor
        self.signals[name] = True

    def final(self) -> float:
        return round(min(0.99, max(0.0, self.score)), 4)


def decide(score: float, margin: float = 1.0) -> str | None:
    """'accepted', 'held', or None (drop).

    `margin` is the gap to the runner-up candidate. A pair that scores highly but
    barely beats an equally plausible alternative is held rather than accepted,
    because the score alone cannot tell those two apart — and picking one at
    random is exactly the wrong-attribution failure this pipeline is built to
    avoid.
    """
    if score >= ACCEPT and margin >= MIN_MARGIN:
        return "accepted"
    if score >= HOLD:
        return "held"
    return None


def name_similarity(a: str, b: str) -> float:
    """First + last name similarity in 0–1, ignoring case, punctuation and middles.

    Compares parsed parts rather than raw strings. The raw-string version scored
    'Gary H. Lyman' against NPPES 'gary herbert lyman' at 0.60 and even
    'Kari C. Nadeau' against 'kari c nadeau' below 1.0 (case and periods), which
    kept nearly every true pair under the accept line (first live run,
    2026-10-10). Middle names are scored separately as agree/conflict evidence.

    Imported lazily so that this module's thresholds and aggregation stay
    importable (and unit-testable) without the native rapidfuzz wheel installed.
    """
    from rapidfuzz import fuzz

    from .names import clean_token, parse_name

    if not a or not b:
        return 0.0
    first_a, _middles_a, last_a = parse_name(a)
    first_b, _middles_b, last_b = parse_name(b)
    if not (first_a and last_a and first_b and last_b):
        # Unparseable (single token, initial-only): fall back to a normalized
        # whole-string comparison.
        norm_a = " ".join(clean_token(t) for t in a.split())
        norm_b = " ".join(clean_token(t) for t in b.split())
        return fuzz.token_sort_ratio(norm_a, norm_b) / 100.0

    last = 1.0 if last_a == last_b else fuzz.ratio(last_a, last_b) / 100.0
    if first_a == first_b:
        first = 1.0
    elif first_a.startswith(first_b) or first_b.startswith(first_a):
        first = 0.9    # Jon / Jonathan, Chris / Christopher
    else:
        first = fuzz.ratio(first_a, first_b) / 100.0
    return round(0.6 * last + 0.4 * first, 3)


def best_name_similarity(candidate_names, target_name: str) -> float:
    """Best match across an author's display name and all its variants.

    OpenAlex fragments one human across spellings ('Robert J. Smith',
    'Bob Smith', 'R.J. Smith'), so scoring only the primary display name
    understates good candidates.
    """
    scores = [name_similarity(n, target_name) for n in candidate_names if n]
    return max(scores) if scores else 0.0


def middle_initial_agreement(a: set, b: set) -> float:
    """+1 when initials overlap, -1 when both exist and conflict, 0 otherwise.

    A conflict is real evidence against: 'John A. Smith' and 'John B. Smith' are
    usually two people. Absence is not, since most sources abbreviate
    inconsistently.
    """
    if not a or not b:
        return 0.0
    return 1.0 if a & b else -1.0


def geo_overlap(candidate_states: set, physician_state: str | None) -> float:
    """1.0 when an author's institutions sit in the physician's practice state.

    State rather than city on purpose: academic physicians move between hospitals
    within a metro area constantly, and affiliation years in OpenAlex lag reality
    by a year or two, so a stricter test mostly generates false negatives.
    """
    if not candidate_states or not physician_state:
        return 0.0
    return 1.0 if physician_state.upper() in {s.upper() for s in candidate_states} else 0.0


@dataclass
class MatchResult:
    """One scored (physician, external identity) pair, ready to persist."""

    npi: str
    external_id: str
    score: float
    status: str
    signals: dict
    margin: float = 0.0
    method: str = "pool_seed"
    orcid: str | None = None
    orcid_confirmed: bool = False


def resolve(
    npi: str,
    scored: list[tuple[str, float, dict]],
    method: str = "pool_seed",
    multi_accept: bool = True,
) -> list[MatchResult]:
    """Turn one physician's scored candidates into decisions.

    `scored` is [(external_id, score, signals), …] for a single NPI.

    Several accepted identities per physician is a feature, not a bug: the
    schema's composite key on (npi, openalex_author_id) exists precisely because
    OpenAlex commonly fragments one human across entities, and re-uniting them is
    the point. So the margin test is not "beat every other candidate" — it is
    "beat the best candidate that is *not itself acceptable*". Two strong
    fragments of the same person shouldn't cancel each other out, while one
    strong candidate shadowed by an equally strong rival still gets held.
    """
    if not scored:
        return []

    ordered = sorted(scored, key=lambda row: row[1], reverse=True)
    strong = [row for row in ordered if row[1] >= ACCEPT]
    # The best score among candidates that are not themselves accept-worthy.
    contender = next((row[1] for row in ordered if row[1] < ACCEPT), 0.0)

    results = []
    for external_id, score, signals in ordered:
        margin = score - contender if score >= ACCEPT else 0.0
        status = decide(score, margin if score >= ACCEPT else 1.0)
        if status is None:
            continue
        if status == "accepted" and not multi_accept and strong and external_id != strong[0][0]:
            status = "held"
        results.append(
            MatchResult(
                npi=npi,
                external_id=external_id,
                score=score,
                status=status,
                signals=dict(signals, margin=round(margin, 3)),
                margin=margin,
                method=method,
                orcid=signals.get("_orcid"),
                orcid_confirmed=bool(signals.get("orcid_confirmed")),
            )
        )
    return results


def summarize_thresholds(scores: list[float]) -> dict:
    """Counts by band — the shape of a matcher run, for the run summary.

    A run with an empty held band is a red flag, not a clean result: it means the
    threshold logic isn't splitting ambiguous cases and something is being
    silently guessed (docs/prerequisites/03-publications.md, sanity checks).
    """
    accepted = sum(1 for s in scores if s >= ACCEPT)
    held = sum(1 for s in scores if HOLD <= s < ACCEPT)
    dropped = sum(1 for s in scores if s < HOLD)
    return {"accepted": accepted, "held": held, "dropped": dropped}
