"""Name normalization — one definition of "same name", shared by buckets 3 and 4.

Both matchers key candidates on a normalized last name plus first initial, and
the trials prerequisite is explicit about why this module exists rather than
each bucket rolling its own:

  "Use the same normalization and the same candidate-lookup code as the
   publications matcher (bucket 3), so the two buckets share one definition of
   'same name' rather than drifting apart."
   — docs/prerequisites/04-clinical-trials.md#name-normalization

Names arrive in three shapes and all three have to reduce to the same key:

    NPPES              first_name='SARFARAZ', last_name='HASNI'
    ClinicalTrials.gov 'Sarfaraz A Hasni, M.D.'
    OpenAlex           'Sarfaraz A. Hasni'          →  key 'hasni|s'

Deliberately stdlib-only: this is the hottest code in the pipeline (the matcher
hits it well over a million times) and it is the code most worth unit-testing,
so it carries no dependency that would stop the tests running.
"""

from __future__ import annotations

import re
import unicodedata

# Credentials and honorifics that trail a name. Matched repeatedly from the end,
# because "Jane Roe, M.D., Ph.D., F.A.C.S." stacks several.
CREDENTIALS = {
    "md", "do", "phd", "dphil", "mbbs", "mbchb", "ms", "msc", "ma", "mph", "mba",
    "rn", "np", "pa", "pac", "dds", "dmd", "dvm", "pharmd", "dnp", "edd", "scd",
    "facs", "facc", "facp", "faap", "faan", "facog", "fasco", "frcs", "frcp",
    "bsc", "bs", "ba", "mhs", "mmsc", "msn", "cnm", "otr", "pt", "dpt", "psyd",
}

# Generational suffixes — part of the name, but never part of the surname.
GENERATIONAL = {"jr", "sr", "ii", "iii", "iv", "v"}

# Surname particles that belong to the surname: "van der Berg" keys on
# "vanderberg", not "berg". Dropping them would split one person into two keys.
PARTICLES = {
    "van", "von", "der", "den", "de", "del", "della", "di", "da", "dos", "das",
    "la", "le", "el", "al", "bin", "ibn", "mac", "mc", "st", "ter", "ten",
}

HONORIFICS = {"dr", "prof", "professor", "mr", "mrs", "ms", "miss", "sir"}


def strip_accents(value: str) -> str:
    """'Muñoz' → 'Munoz'.

    Sources disagree about diacritics for the same human — OpenAlex usually keeps
    them, NPPES usually does not — so folding them is what lets the two match.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def clean_token(token: str) -> str:
    """Lowercase a single name token and drop everything that isn't a letter."""
    return re.sub(r"[^a-z]", "", strip_accents(token).lower())


def _words(raw: str) -> list[tuple[str, bool]]:
    """Split into (cleaned token, is_initial_cluster), dropping noise words.

    **Periods are removed per word, never used as separators.** That ordering is
    load-bearing: splitting on periods first turns "M.D." into two tokens "M" and
    "D", which then fail the credential test and get mistaken for a given name —
    so "Jane Roe, M.D." would inv­ert into a doctor named "M D Jane Roe".

    `is_initial_cluster` records that a token came from a dotted abbreviation
    ("Y.H." → 'yh'), which is what lets `parse_name` expand it into two initials
    while leaving a spelled-out middle name like "Yee" as one.
    """
    if not raw:
        return []

    text = strip_accents(raw).strip()

    # 'Hasni, Sarfaraz A' → 'Sarfaraz A Hasni'. But leave 'Hasni, M.D.' alone:
    # there the tail is credentials, not a given name.
    if "," in text:
        head, _, tail = text.partition(",")
        tail_tokens = [t for t in (clean_token(w) for w in tail.split()) if t]
        if tail_tokens and not all(t in CREDENTIALS | GENERATIONAL for t in tail_tokens):
            text = tail + " " + head

    words = []
    for word in re.split(r"[\s,]+", text):
        token = clean_token(word)
        if not token:
            continue
        if token in CREDENTIALS or token in GENERATIONAL or token in HONORIFICS:
            continue
        words.append((token, "." in word and len(token) <= 4))
    return words


def tokenize(raw: str) -> list[str]:
    """Name tokens with credentials, honorifics and suffixes removed."""
    return [token for token, _cluster in _words(raw)]


def parse_name(raw: str) -> tuple[str | None, set[str], str | None]:
    """Split a display name into (first, middle_initials, last).

    Middle names collapse to a set of initials, so 'Y.H.' and 'Yee Hoong' both
    reduce to {'y','h'} and compare cleanly across sources that abbreviate
    differently. The same reduction is used by scripts/bootstrap_eval_set.py.
    """
    words = _words(raw)
    if len(words) < 2:
        return None, set(), None
    tokens = [token for token, _ in words]

    first = tokens[0]
    # Absorb surname particles into the surname: 'van der Berg' keys on the whole
    # surname, not on 'berg'.
    last_parts = [tokens[-1]]
    idx = len(tokens) - 2
    while idx > 0 and tokens[idx] in PARTICLES:
        last_parts.insert(0, tokens[idx])
        idx -= 1
    last = "".join(last_parts)

    middles = set()
    for token, is_cluster in words[1:idx + 1]:
        if is_cluster:
            middles.update(token)      # 'yh' → {'y', 'h'}
        elif token:
            middles.add(token[0])      # 'yee' → {'y'}

    if len(first) < 2 or len(last) < 2:
        return None, set(), None
    return first, middles, last


def name_key(raw: str) -> str | None:
    """The candidate-lookup key: normalized surname + first initial.

    'Sarfaraz A Hasni, M.D.' → 'hasni|s'

    Broad and dumb on purpose. This key only has to *retrieve* candidates cheaply
    — precision comes from scoring afterwards. Making it stricter (full first
    name, say) would silently drop the 'Robert'/'Bob' and 'Jonathan'/'Jon' cases
    that the scorer is perfectly able to judge.

    Returns None when there is no usable key; callers must treat that as "not a
    candidate for anything" rather than as an empty-string key, which is why both
    staging indexes are partial on `name_key IS NOT NULL`.
    """
    first, _middles, last = parse_name(raw)
    if not first or not last:
        return None
    return f"{last}|{first[0]}"


def name_key_parts(first: str, last: str) -> str | None:
    """The same key, built from already-separated fields (the NPPES case)."""
    first_clean = clean_token(first or "")
    last_tokens = [clean_token(t) for t in (last or "").split()]
    last_tokens = [t for t in last_tokens if t and t not in GENERATIONAL]
    last_clean = "".join(last_tokens)
    if len(first_clean) < 2 or len(last_clean) < 2:
        return None
    return f"{last_clean}|{first_clean[0]}"


def name_keys_for_physician(
    first: str, last: str, other_names: list | None = None
) -> set[str]:
    """Every key a physician could be found under, including alternate names.

    NPPES carries prior/alternate names (maiden names, variants) and the
    publications runbook is explicit that the candidate lookup must include them
    — a surgeon who published under a maiden name is otherwise invisible to the
    matcher, which is the single most common cause of a false negative.
    """
    keys = set()
    primary = name_key_parts(first, last)
    if primary:
        keys.add(primary)

    for entry in other_names or []:
        if isinstance(entry, dict):
            alt_first = entry.get("first_name") or entry.get("first") or first
            alt_last = entry.get("last_name") or entry.get("last") or ""
            key = name_key_parts(alt_first, alt_last)
        else:
            key = name_key(str(entry))
        if key:
            keys.add(key)
    return keys


def full_name(first: str, middle: str = "", last: str = "") -> str:
    """A display/comparison string, e.g. 'sarfaraz a hasni'."""
    parts = [clean_token(p) for p in (first, middle, last)]
    return " ".join(p for p in parts if p)


def initials_of(value: str) -> set[str]:
    """Middle-name initials as a set: 'A' → {'a'}, 'Ann Marie' → {'a','m'}."""
    return {clean_token(t)[0] for t in (value or "").split() if clean_token(t)}
