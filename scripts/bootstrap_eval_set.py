#!/usr/bin/env python3
"""Bootstrap the physician <-> OpenAlex-author gold-standard eval set.

Produces a *seed* eval set automatically instead of hand-curating 200 pairs from
scratch. The output is not ground truth until a human spot-checks it -- every row
carries its confidence, the signals that fired, and two click-through URLs so a
reviewer can confirm or reject in seconds.

Direction of travel matters. The production matcher runs
physician -> candidate authors, which is the hard direction (one NPI, many
same-named authors). This bootstrapper runs the *other* way: it starts from
well-published US authors in a given medical specialty and looks them up in
NPPES. That inverts the ambiguity -- a prolific, named author with a rare name
and a matching specialty resolves to exactly one NPI far more often than a
randomly chosen physician resolves to one author.

Sources, both free and unauthenticated:
  - OpenAlex   https://api.openalex.org      (polite pool via CONTACT_EMAIL)
  - NPPES      https://npiregistry.cms.hhs.gov/api/

Usage:
    python3 scripts/bootstrap_eval_set.py                 # full run, ~200 pairs
    python3 scripts/bootstrap_eval_set.py --target 50     # smaller run
    python3 scripts/bootstrap_eval_set.py --no-cache      # ignore the HTTP cache

Reads CONTACT_EMAIL and PEX_DATA_DIR from the environment (or a repo-root .env).
Stdlib only, so it runs before `uv add` has been done.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

GENERATOR_VERSION = "0.1.0"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO_ROOT, "eval")
OUT_PAIRS = os.path.join(OUT_DIR, "physician_author_pairs.csv")
OUT_HELD = os.path.join(OUT_DIR, "held_ambiguous.csv")

OPENALEX = "https://api.openalex.org"
NPPES = "https://npiregistry.cms.hhs.gov/api/"

# Emit threshold. Below this a pair is written to held_ambiguous.csv instead.
MIN_CONFIDENCE = 0.70
# A best candidate this close to the runner-up is ambiguous, not a match.
MIN_MARGIN = 0.10
# Authors below this are not "well-published" enough to be worth a lookup.
MIN_WORKS = 40
# Above this, an OpenAlex author entity is almost certainly a mis-grouped blob
# (the docs' "Amazon (2025)" caveat) rather than one human being.
MAX_WORKS = 4000

# OpenAlex subfield -> (label, NPPES taxonomy keywords).
# The keywords are matched case-insensitively as substrings against the NPI's
# primary taxonomy description, so they stay robust to NUCC wording changes.
SPECIALTIES = [
    ("2705", "Cardiology",            ["cardiovascular disease", "cardiac", "cardiology"]),
    ("2730", "Oncology",              ["oncology", "hematology & oncology"]),
    ("2746", "Surgery",               ["surgery"]),
    ("2728", "Neurology",             ["neurology", "neuromuscular"]),
    ("2708", "Dermatology",           ["dermatology"]),
    ("2732", "Orthopedics",           ["orthopaedic", "orthopedic", "sports medicine"]),
    ("2729", "Obstetrics & Gynecology", ["obstetrics", "gynecology"]),
    ("2735", "Pediatrics",            ["pediatric"]),
    ("2738", "Psychiatry",            ["psychiatry"]),
    ("2715", "Gastroenterology",      ["gastroenterology"]),
    ("2748", "Urology",               ["urology"]),
    ("2731", "Ophthalmology",         ["ophthalmology"]),
    ("2712", "Endocrinology",         ["endocrinology", "diabetes"]),
    ("2727", "Nephrology",            ["nephrology"]),
    ("2740", "Pulmonology",           ["pulmonary"]),
    ("2745", "Rheumatology",          ["rheumatology"]),
    ("2703", "Anesthesiology",        ["anesthesiology", "pain medicine"]),
    ("2741", "Radiology",             ["radiology", "nuclear medicine"]),
    ("2725", "Infectious Disease",    ["infectious disease"]),
    ("2720", "Hematology",            ["hematology"]),
    ("2733", "Otolaryngology",        ["otolaryngology"]),
    ("2723", "Allergy & Immunology",  ["allergy", "immunology"]),
    ("2711", "Emergency Medicine",    ["emergency medicine"]),
    ("2714", "Family Practice",       ["family medicine"]),
    ("2724", "Internal Medicine",     ["internal medicine"]),
    ("2717", "Geriatrics",            ["geriatric"]),
    ("2706", "Critical Care",         ["critical care", "intensive care"]),
    ("2747", "Transplantation",       ["transplant"]),
]

NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "phd", "do", "mph", "msc", "facs", "facc"}

# Order matters: the first keyword hit wins, so subspecialties must be tested
# before the generic parents they sit under ("Orthopaedic Surgery" must land in
# Orthopedics, not Surgery; "Internal Medicine, Nephrology" in Nephrology).
GENERIC_BUCKETS = {"Surgery", "Internal Medicine", "Family Practice", "Pediatrics"}


def bucket_from_taxonomy(taxonomy: str) -> str:
    """Label a pair by what the physician *practices*, per NPPES.

    The specialty a pair was discovered under is only provenance -- prolific
    authors publish across many subfields, so the discovering query says little
    about their actual practice. NPPES taxonomy is the authoritative answer, and
    using it is what makes a specialty-stratified precision number meaningful.
    """
    tax = (taxonomy or "").lower()
    if not tax:
        return "Unknown"
    specific = [s for s in SPECIALTIES if s[1] not in GENERIC_BUCKETS]
    generic = [s for s in SPECIALTIES if s[1] in GENERIC_BUCKETS]
    for _sid, label, keywords in specific + generic:
        if any(kw in tax for kw in keywords):
            return label
    return taxonomy.split(",")[0].strip() or "Unknown"


# --------------------------------------------------------------------------
# environment + HTTP
# --------------------------------------------------------------------------

def load_dotenv() -> None:
    """Minimal .env reader so this runs without python-dotenv installed."""
    path = os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


class Http:
    """Polite HTTP with on-disk caching and retry.

    The cache lives under PEX_DATA_DIR (never in the repo, per the ground rules
    in docs/prerequisites/README.md) and makes re-runs effectively free, which
    matters because tuning the scorer means running this many times.
    """

    def __init__(self, contact: str, cache_dir: str, use_cache: bool = True):
        self.contact = contact
        self.cache_dir = cache_dir
        self.use_cache = use_cache
        self.calls = 0
        self.cache_hits = 0
        if use_cache:
            os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, url: str) -> str:
        return os.path.join(self.cache_dir, hashlib.sha256(url.encode()).hexdigest() + ".json")

    def get(self, url: str, pause: float = 0.12, tries: int = 4):
        if self.use_cache:
            path = self._cache_path(url)
            if os.path.exists(path):
                self.cache_hits += 1
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)

        last = None
        for attempt in range(tries):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "pex-eval-bootstrap/%s (%s)" % (GENERATOR_VERSION, self.contact)}
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.load(resp)
                self.calls += 1
                if self.use_cache:
                    with open(self._cache_path(url), "w", encoding="utf-8") as fh:
                        json.dump(data, fh)
                time.sleep(pause)
                return data
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt)
                    continue
                raise
            except Exception as exc:  # network flake
                last = exc
                time.sleep(2 ** attempt)
        raise RuntimeError("GET failed after %d tries: %s (%s)" % (tries, url, last))


# --------------------------------------------------------------------------
# name handling
# --------------------------------------------------------------------------

def parse_name(display_name: str):
    """Split an OpenAlex display name into (first, middle_initials, last).

    OpenAlex names arrive as 'Gregory Y.H. Lip', 'John J.V. McMurray',
    'Bruce M. Psaty'. Initials are kept as a set of letters so 'Y.H.' and
    'Yee Hoong' both reduce to {y, h} and compare cleanly against NPPES.
    """
    cleaned = re.sub(r"[^A-Za-z\s\.\-']", " ", display_name or "")
    tokens = [t for t in cleaned.replace(".", ". ").split() if t.strip(".")]
    tokens = [t for t in tokens if t.strip(".").lower() not in NAME_SUFFIXES]
    if len(tokens) < 2:
        return None, set(), None
    first = tokens[0].strip(".")
    last = tokens[-1].strip(".")
    middles = set()
    for tok in tokens[1:-1]:
        for ch in tok.strip("."):
            if ch.isalpha():
                middles.add(ch.lower())
    if len(first) < 2 or len(last) < 2:
        return None, set(), None
    return first, middles, last


def middle_initials(value: str):
    return {ch.lower() for ch in re.sub(r"[^A-Za-z]", "", value or "")[:4]} if value else set()


# --------------------------------------------------------------------------
# OpenAlex
# --------------------------------------------------------------------------

def topic_ids_for_subfield(http: Http, subfield_id: str, mailto: str, limit: int = 12):
    url = "%s/topics?filter=subfield.id:subfields/%s&sort=works_count:desc&per-page=%d&mailto=%s" % (
        OPENALEX, subfield_id, limit, urllib.parse.quote(mailto)
    )
    data = http.get(url)
    return [t["id"].rsplit("/", 1)[-1] for t in data.get("results", [])]


def authors_for_topics(http: Http, topic_ids, mailto: str, per_page: int = 50):
    """Well-published US authors on these topics, most-cited first.

    `sort=summary_stats.h_index:desc` is what delivers the requested bias toward
    well-known physicians. The works_count band excludes both thin author stubs
    and the mis-grouped mega-entities OpenAlex occasionally produces.
    """
    flt = ",".join([
        "topics.id:" + "|".join(topic_ids),
        "affiliations.institution.country_code:US",
        "works_count:>%d" % MIN_WORKS,
    ])
    url = "%s/authors?filter=%s&sort=summary_stats.h_index:desc&per-page=%d&mailto=%s" % (
        OPENALEX, flt, per_page, urllib.parse.quote(mailto)
    )
    return http.get(url).get("results", [])


def institution_geo(http: Http, inst_ids, mailto: str, cache: dict):
    """Resolve OpenAlex institution IDs to (name, city, state). Batched 50 at a time."""
    missing = [i for i in inst_ids if i not in cache]
    for start in range(0, len(missing), 50):
        chunk = missing[start:start + 50]
        url = "%s/institutions?filter=ids.openalex:%s&per-page=50&mailto=%s" % (
            OPENALEX, "|".join(chunk), urllib.parse.quote(mailto)
        )
        try:
            data = http.get(url)
        except Exception:
            continue
        for inst in data.get("results", []):
            geo = inst.get("geo") or {}
            cache[inst["id"].rsplit("/", 1)[-1]] = (
                inst.get("display_name"),
                geo.get("city"),
                geo.get("region"),
                geo.get("country_code"),
            )
    # Negative-cache anything the API did not return, so it is not re-requested.
    for iid in missing:
        cache.setdefault(iid, (None, None, None, None))
    return cache


def author_affiliations(author):
    """Most-recent-first affiliation list as (openalex_inst_id, latest_year)."""
    out = []
    for aff in author.get("affiliations", []) or []:
        inst = aff.get("institution") or {}
        iid = (inst.get("id") or "").rsplit("/", 1)[-1]
        years = aff.get("years") or []
        if iid:
            out.append((iid, max(years) if years else 0))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


# --------------------------------------------------------------------------
# NPPES
# --------------------------------------------------------------------------

def nppes_lookup(http: Http, first: str, last: str):
    url = NPPES + "?" + urllib.parse.urlencode({
        "version": "2.1",
        "enumeration_type": "NPI-1",
        "first_name": first,
        "last_name": last,
        "limit": 200,
    })
    try:
        data = http.get(url, pause=0.18)
    except Exception:
        return 0, []
    return data.get("result_count", 0), data.get("results", []) or []


def nppes_facts(record):
    basic = record.get("basic", {}) or {}
    primary = None
    for tax in record.get("taxonomies", []) or []:
        if tax.get("primary"):
            primary = tax
            break
    if primary is None and record.get("taxonomies"):
        primary = record["taxonomies"][0]
    primary = primary or {}

    location = None
    for addr in record.get("addresses", []) or []:
        if addr.get("address_purpose") == "LOCATION":
            location = addr
            break
    location = location or (record.get("addresses") or [{}])[0]

    states = set()
    if location.get("state"):
        states.add(location["state"].upper())
    if primary.get("state"):
        states.add(primary["state"].upper())
    for practice in record.get("practiceLocations", []) or []:
        if practice.get("state"):
            states.add(practice["state"].upper())

    return {
        "npi": record.get("number"),
        "first": (basic.get("first_name") or "").strip(),
        "middle": (basic.get("middle_name") or "").strip(),
        "last": (basic.get("last_name") or "").strip(),
        "credential": (basic.get("credential") or "").strip(),
        "status": (basic.get("status") or "").strip(),
        "taxonomy": (primary.get("desc") or "").strip(),
        "taxonomy_code": (primary.get("code") or "").strip(),
        "city": (location.get("city") or "").strip().title(),
        "state": (location.get("state") or "").strip().upper(),
        "states": states,
        "other_names": [
            ((o.get("first_name") or "") + " " + (o.get("last_name") or "")).strip()
            for o in (record.get("other_names") or [])
        ],
    }


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def score_candidate(author, oa_middles, oa_states, cand, keywords, namesakes):
    """Score one NPPES record against one OpenAlex author. Returns (score, signals).

    Signals are additive and deliberately independent of each other, so a single
    weak source cannot carry a pair over the line on its own.
    """
    score = 0.0
    signals = []

    taxonomy = (cand["taxonomy"] or "").lower()
    if any(kw in taxonomy for kw in keywords):
        score += 0.30
        signals.append("specialty")

    if oa_states and cand["states"] & oa_states:
        score += 0.20
        signals.append("state")

    # Name rarity, measured in the registry itself rather than assumed.
    if namesakes == 1:
        score += 0.25
        signals.append("unique-name")
    elif namesakes == 2:
        score += 0.15
        signals.append("rare-name:2")
    elif namesakes <= 4:
        score += 0.08
        signals.append("rare-name:%d" % namesakes)
    elif namesakes <= 10:
        score += 0.03
        signals.append("common-name:%d" % namesakes)
    else:
        signals.append("very-common-name:%d%s" % (namesakes, "+" if namesakes >= 200 else ""))

    nppes_middles = middle_initials(cand["middle"])
    if oa_middles and nppes_middles:
        if oa_middles & nppes_middles:
            score += 0.12
            signals.append("middle-initial")
        else:
            score -= 0.15
            signals.append("middle-conflict")

    # Exact-token match, not substring: a naive "MD" in cred also lights up for
    # "DMD" (dentist) and "PharmD" (pharmacist), both of which contain "MD" as
    # a substring but are not MD/DO credentials.
    cred_tokens = re.split(r"[^A-Z]+", cand["credential"].upper().replace(".", ""))
    if "MD" in cred_tokens or "DO" in cred_tokens:
        score += 0.08
        signals.append("md-do")

    if author.get("orcid"):
        score += 0.05
        signals.append("orcid")

    h_index = (author.get("summary_stats") or {}).get("h_index") or 0
    if h_index >= 30:
        score += 0.05
        signals.append("h%d" % h_index)

    if cand["status"] != "A":
        score -= 0.10
        signals.append("inactive-npi")

    return max(0.0, min(0.99, score)), signals


def acceptable(score, margin, signals):
    """Gate on top of the raw score.

    A high score built purely on name rarity is not enough -- the pair needs at
    least one piece of corroborating domain evidence (specialty or geography),
    and it needs to beat its runner-up clearly.
    """
    if score < MIN_CONFIDENCE:
        return False
    if margin < MIN_MARGIN:
        return False
    return "specialty" in signals or "state" in signals


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

PAIR_FIELDS = [
    "npi", "physician_name", "nppes_specialty", "nppes_city", "nppes_state", "nppes_credential",
    "openalex_author_id", "openalex_name", "openalex_orcid", "openalex_works_count",
    "openalex_h_index", "openalex_cited_by_count", "openalex_top_subfield",
    "openalex_recent_institution", "specialty_bucket", "query_specialty",
    "confidence", "signals", "nppes_namesakes", "runner_up_margin",
    "npi_url", "openalex_url",
    "verdict", "reviewer", "reviewer_notes", "generated_at", "generator_version",
]


def build_row(author, cand, query_bucket, score, signals, namesakes, margin, inst_name, subfield):
    stats = author.get("summary_stats") or {}
    return {
        "npi": cand["npi"],
        "physician_name": " ".join(x for x in [cand["first"], cand["middle"], cand["last"]] if x).title(),
        "nppes_specialty": cand["taxonomy"],
        "nppes_city": cand["city"],
        "nppes_state": cand["state"],
        "nppes_credential": cand["credential"],
        "openalex_author_id": author["id"].rsplit("/", 1)[-1],
        "openalex_name": author.get("display_name", ""),
        "openalex_orcid": (author.get("orcid") or "").replace("https://orcid.org/", ""),
        "openalex_works_count": author.get("works_count", 0),
        "openalex_h_index": stats.get("h_index") or 0,
        "openalex_cited_by_count": author.get("cited_by_count", 0),
        "openalex_top_subfield": subfield,
        "openalex_recent_institution": inst_name or "",
        "specialty_bucket": bucket_from_taxonomy(cand["taxonomy"]),
        "query_specialty": query_bucket,
        "confidence": round(score, 3),
        "signals": " ".join(signals),
        "nppes_namesakes": namesakes,
        "runner_up_margin": round(margin, 3),
        "npi_url": "https://npiregistry.cms.hhs.gov/provider-view/" + str(cand["npi"]),
        "openalex_url": author["id"],
        "verdict": "",
        "reviewer": "",
        "reviewer_notes": "",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generator_version": GENERATOR_VERSION,
    }


def main():
    ap = argparse.ArgumentParser(description="Bootstrap the physician<->author eval set.")
    ap.add_argument("--target", type=int, default=200, help="how many pairs to emit (default 200)")
    ap.add_argument("--per-specialty", type=int, default=50, help="OpenAlex authors to consider per specialty")
    ap.add_argument("--no-cache", action="store_true", help="bypass the on-disk HTTP cache")
    args = ap.parse_args()

    load_dotenv()
    contact = os.environ.get("CONTACT_EMAIL", "").strip()
    if not contact or contact == "you@example.com":
        sys.exit("CONTACT_EMAIL must be set (in .env or the environment) -- both APIs ask for it.")

    data_dir = os.environ.get("PEX_DATA_DIR", os.path.expanduser("~/pex-data"))
    cache_dir = os.path.join(data_dir, "_cache", "eval-bootstrap")
    http = Http(contact, cache_dir, use_cache=not args.no_cache)

    os.makedirs(OUT_DIR, exist_ok=True)
    inst_cache: dict = {}
    pool = []
    held = []
    seen_npi = set()
    seen_author = set()

    # Collect generously from every specialty query, then let the selection stage
    # enforce the spread. Capping during collection would file a cardiologist
    # under whichever query happened to reach them first.
    per_specialty_cap = max(4, (args.target // len(SPECIALTIES) + 1) * 3)

    for subfield_id, bucket, keywords in SPECIALTIES:
        try:
            topic_ids = topic_ids_for_subfield(http, subfield_id, contact)
            if not topic_ids:
                print("  ! no topics for %s" % bucket, file=sys.stderr)
                continue
            authors = authors_for_topics(http, topic_ids, contact, args.per_specialty)
        except Exception as exc:
            print("  ! %s: %s" % (bucket, exc), file=sys.stderr)
            continue

        # Resolve every institution for this batch in one pass.
        needed = []
        for author in authors:
            needed.extend(iid for iid, _ in author_affiliations(author)[:5])
        try:
            institution_geo(http, sorted(set(needed)), contact, inst_cache)
        except Exception:
            pass

        accepted = 0
        for author in authors:
            if accepted >= per_specialty_cap:
                break
            aid = author["id"].rsplit("/", 1)[-1]
            if aid in seen_author:
                continue
            works = author.get("works_count", 0)
            if works < MIN_WORKS or works > MAX_WORKS:
                continue

            first, oa_middles, last = parse_name(author.get("display_name", ""))
            if not first or not last:
                continue

            affils = author_affiliations(author)[:5]
            oa_states = set()
            recent_inst = None
            for iid, _year in affils:
                name, _city, region, country = inst_cache.get(iid, (None, None, None, None))
                if recent_inst is None and name:
                    recent_inst = name
                if country == "US" and region:
                    oa_states.add(str(region).upper()[:2])

            namesakes, records = nppes_lookup(http, first, last)
            if not records:
                continue

            subfield = ""
            topics = author.get("topics") or []
            if topics:
                subfield = (topics[0].get("subfield") or {}).get("display_name", "")

            scored = []
            for record in records:
                cand = nppes_facts(record)
                if not cand["npi"] or cand["npi"] in seen_npi:
                    continue
                # No taxonomy means no specialty label, which makes the row
                # useless for a specialty-stratified precision measurement.
                if not cand["taxonomy"]:
                    continue
                # This is a *physician* <-> author eval set. NUCC taxonomy codes
                # for the "Allopathic & Osteopathic Physicians" grouping all
                # start with "20" (MD and DO alike); dentists, pharmacists,
                # podiatrists, etc. use other prefixes. Filtering here -- rather
                # than trusting the credential string -- is what catches a DMD
                # or PharmD before "MD" in cred can misfire on it (see the
                # score_candidate docstring).
                if not cand["taxonomy_code"].startswith("20"):
                    continue
                score, signals = score_candidate(
                    author, oa_middles, oa_states, cand, keywords, namesakes
                )
                scored.append((score, signals, cand))
            if not scored:
                continue

            scored.sort(key=lambda x: x[0], reverse=True)
            best_score, best_signals, best_cand = scored[0]
            runner_up = scored[1][0] if len(scored) > 1 else 0.0
            margin = best_score - runner_up

            row = build_row(author, best_cand, bucket, best_score, best_signals,
                            namesakes, margin, recent_inst, subfield)

            if acceptable(best_score, margin, best_signals):
                pool.append(row)
                seen_npi.add(best_cand["npi"])
                seen_author.add(aid)
                accepted += 1
            elif best_score >= 0.45:
                held.append(row)

        print("  queried %-24s %2d candidates" % (bucket, accepted), file=sys.stderr)

    # Selection: round-robin over the NPPES-derived specialty so the spread is
    # real rather than an artifact of which query found each author first.
    by_specialty = defaultdict(list)
    for row in pool:
        by_specialty[row["specialty_bucket"]].append(row)
    for rows in by_specialty.values():
        rows.sort(key=lambda r: r["confidence"], reverse=True)

    ordered = []
    buckets = [by_specialty[k] for k in sorted(by_specialty)]
    idx = 0
    while len(ordered) < args.target and any(idx < len(b) for b in buckets):
        for b in buckets:
            if idx < len(b) and len(ordered) < args.target:
                ordered.append(b[idx])
        idx += 1

    with open(OUT_PAIRS, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PAIR_FIELDS)
        writer.writeheader()
        for row in sorted(ordered, key=lambda r: (r["specialty_bucket"], -r["confidence"])):
            writer.writerow(row)

    with open(OUT_HELD, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PAIR_FIELDS)
        writer.writeheader()
        for row in sorted(held, key=lambda r: -r["confidence"]):
            writer.writerow(row)

    dist = defaultdict(int)
    for row in ordered:
        dist[row["specialty_bucket"]] += 1
    print("\nspecialty spread (from NPPES taxonomy):", file=sys.stderr)
    for label in sorted(dist, key=lambda k: -dist[k]):
        print("  %-34s %3d" % (label, dist[label]), file=sys.stderr)

    if ordered:
        avg = sum(r["confidence"] for r in ordered) / len(ordered)
        print("\nmean confidence %.3f | min %.3f" % (
            avg, min(r["confidence"] for r in ordered)), file=sys.stderr)
    print("wrote %d pairs -> %s" % (len(ordered), OUT_PAIRS), file=sys.stderr)
    print("wrote %d held/ambiguous -> %s" % (len(held), OUT_HELD), file=sys.stderr)
    print("http: %d calls, %d cache hits" % (http.calls, http.cache_hits), file=sys.stderr)


if __name__ == "__main__":
    main()
