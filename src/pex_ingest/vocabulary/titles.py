"""Match paper and trial titles against every concept's terms in one pass.

Each title is split into words, and every run of 1–6 consecutive words is looked
up in one dictionary of all terms. The cost per title doesn't depend on how many
terms or concepts exist, so 5.7M titles take about a minute per core rather than
being compared against each concept in turn (backend/ranking-formula.md, "When
the matching runs").

Normalization, applied identically to terms and titles:
  * lowercase; anything that isn't a letter or digit becomes a word break, so
    "Anderson-Hynes" matches "anderson hynes" and "ureteropelvic-junction";
  * light plural folding per word ("pyeloplasties" → "pyeloplasty",
    "calculi" stays), so a term never needs its plural spelled out.

Short abbreviations are the precision trap: "MS" is multiple sclerosis and mass
spectrometry. Two-letter terms are never matched. Three-letter single-word terms
("BPH", "UPJ") only match when the title prints them in capitals, and so do
single-word terms of up to five letters that the vocabulary lists as
abbreviations ("TURP", "MESA") — "mesa" in lowercase is an ordinary word, and even
in capitals the literature check still has to catch "MESA", the heart study.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

MAX_WORDS = 6
_SPLIT = re.compile(r"[^0-9a-z]+")
_SPLIT_CASED = re.compile(r"[^0-9A-Za-z]+")

# Words whose trailing "s" is not a plural; folding them would merge real words.
_KEEP_S = {
    "is", "as", "us", "was", "has", "this", "its", "his", "yes", "gas", "bias", "plus",
    "basis", "stenosis", "sclerosis", "fibrosis", "thrombosis", "psoriasis",
    "lithiasis", "nephrolithiasis", "urolithiasis", "cholelithiasis",
    "diagnosis", "prognosis", "analysis", "dialysis", "paralysis", "sepsis",
    "metastasis", "hydronephrosis", "endometriosis", "osteoporosis", "scoliosis",
    "ptosis", "arthrosis", "neurosis", "psychosis", "tuberculosis", "mellitus",
    "virus", "fetus", "uterus", "meniscus", "sinus", "bronchus", "calculus",
    "pancreas", "status", "corpus", "lupus", "herpes", "diabetes", "testis",
    "pubis", "pelvis", "glottis", "epididymis", "cervicitis", "bypass", "mass",
    "class", "less", "loss", "stress", "access", "process", "abscess", "illness",
    "fitness", "thickness", "consensus", "versus", "hiatus", "anus", "humerus",
    "femoris", "biceps", "triceps", "physis", "axis", "iris", "cirrhosis",
    "ascites", "rickets", "mumps", "measles", "aids", "always", "series", "species",
}


def fold(word: str) -> str:
    """Singular form of one lowercase word, conservatively."""
    if len(word) <= 3 or word in _KEEP_S or not word.endswith("s"):
        return word
    if word.endswith("itis") or word.endswith("osis") or word.endswith("ss"):
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"           # pyeloplasties → pyeloplasty
    if word.endswith(("ches", "shes", "xes", "zes", "sses")):
        return word[:-2]                 # stitches → stitch
    if word.endswith("us") or word.endswith("is"):
        return word
    return word[:-1]                     # stones → stone


def words(text: str) -> list[str]:
    return [fold(w) for w in _SPLIT.split((text or "").lower()) if w]


def normalize(text: str) -> str:
    """The canonical form of a term or name: folded words joined by spaces."""
    return " ".join(words(text))


class TermIndex:
    """{word tuple: [(concept_id, tier, is_name), …]} plus the capitals-only set."""

    def __init__(self):
        self.phrases: dict[tuple[str, ...], list[tuple[int, int, bool]]] = defaultdict(list)
        self.capitals: dict[str, list[tuple[int, int, bool]]] = defaultdict(list)

    def add(self, concept_id: int, term: str, tier: int, is_name: bool,
            abbreviation: bool = False) -> bool:
        """Register a term; False when it is too short to match safely.

        `abbreviation`: the vocabulary lists this term in capitals, so a single
        word of up to five letters only matches when a title capitalizes it.
        """
        key = term_key(term, abbreviation)
        if key is None:
            return False
        if isinstance(key, str):
            self.capitals[key].append((concept_id, tier, is_name))
        else:
            self.phrases[key].append((concept_id, tier, is_name))
        return True

    def match(self, title: str) -> dict[int, tuple[int, bool]]:
        """{concept_id: (best tier, name hit)} for one title."""
        hits: dict[int, tuple[int, bool]] = {}

        def record(entries):
            for concept_id, tier, is_name in entries:
                best = hits.get(concept_id)
                name_hit = is_name and tier == 1
                if best is None:
                    hits[concept_id] = (tier, name_hit)
                else:
                    hits[concept_id] = (min(best[0], tier), best[1] or name_hit)

        tokens = words(title)
        phrases = self.phrases
        for start in range(len(tokens)):
            for size in range(1, MAX_WORDS + 1):
                end = start + size
                if end > len(tokens):
                    break
                entries = phrases.get(tuple(tokens[start:end]))
                if entries:
                    record(entries)
        if self.capitals:
            for token in _SPLIT_CASED.split(title or ""):
                if 3 <= len(token) <= MAX_CAPS and token.isupper():
                    entries = self.capitals.get(fold_caps(token))
                    if entries:
                        record(entries)
        return hits

    def term_hits(self, title: str) -> set[tuple[str, ...] | str]:
        """Which registered keys a title contains — for per-term counting in checks."""
        found: set = set()
        tokens = words(title)
        for start in range(len(tokens)):
            for size in range(1, MAX_WORDS + 1):
                end = start + size
                if end > len(tokens):
                    break
                key = tuple(tokens[start:end])
                if key in self.phrases:
                    found.add(key)
        for token in _SPLIT_CASED.split(title or ""):
            if 3 <= len(token) <= MAX_CAPS and token.isupper():
                token = fold_caps(token)
                if token in self.capitals:
                    found.add(token)
        return found


MAX_CAPS = 5


def fold_caps(token: str) -> str:
    """'TURPs' never reaches here (not all capitals); 'UTIS' → 'UTI'."""
    return token[:-1] if len(token) > 3 and token.endswith("S") else token


def term_key(term: str, abbreviation: bool = False) -> tuple[str, ...] | str | None:
    """The key a term is stored under in a TermIndex: a word tuple, or a
    capitals-only string for short abbreviations, or None when unmatchable."""
    tokens = tuple(words(term))
    if not tokens or len(tokens) > MAX_WORDS:
        return None
    if len(tokens) == 1:
        token = tokens[0]
        if len(token) <= 2:
            return None
        if len(token) == 3 or (abbreviation and len(token) <= MAX_CAPS and token.isalnum()):
            return fold_caps(_SPLIT.sub("", (term or "").lower()).upper())
    return tokens


def build_index(terms: Iterable[tuple]) -> TermIndex:
    """From (concept_id, term, tier, is_name[, abbreviation]) rows."""
    index = TermIndex()
    for row in terms:
        index.add(*row)
    return index
