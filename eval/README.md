# Matcher eval set

The measuring stick for the physician↔author matcher's precision. QA tooling, not
profile data — which is why these CSVs live in the repo while every other data
file lives in `$PEX_DATA_DIR`.

| File | Rows | What it is |
|---|---:|---|
| `physician_author_pairs.csv` | 200 | The seed eval set — high-confidence NPI ↔ OpenAlex author pairs |
| `held_ambiguous.csv` | 566 | Pairs that scored 0.45–0.70 or lost to a close runner-up. Hard cases, kept deliberately |

Generated `2026-08-15` by [`scripts/bootstrap_eval_set.py`](../scripts/bootstrap_eval_set.py) v0.1.0.

## Status: seed, not gold standard

**These rows have not been human-reviewed yet.** Until a sample is spot-checked
and verdicts are recorded, this is a *seed* set. Measuring the matcher against an
unreviewed seed measures the bootstrapper against itself.

## Why it was generated rather than hand-curated

[ADR 0001](https://github.com/davidcohn22/physician-expertise-docs/blob/main/architecture-decision-records/0001-v1-data-bucket-strategy.md)
originally called for "200 hand-verified pairs." Building those by hand is 200
manual lookups — the same per-record manual labor the ADR rules out elsewhere.

The generated set is credible because **it runs the lookup backwards**. The
production matcher goes physician → candidate authors, which is the hard
direction: one NPI, and up to 1,160 OpenAlex authors named "Michael Smith." The
bootstrapper goes author → NPI, starting from well-published US authors inside a
given specialty. That inverts the ambiguity — a prolific author with a
distinctive name, a matching NPPES specialty, and a matching practice state
resolves to exactly one NPI far more often than the reverse.

This also means the bootstrapper is **not** the production matcher with a
different name. It uses evidence the production matcher cannot use at scale
(per-name NPPES registry lookups, registry-measured name rarity) and it is tuned
for precision at very low recall — it looked at ~4,000 authors to keep 200 pairs.

## The two caveats that travel with this file

**1. Precision measured here is an upper bound, not the national rate.**
The bias toward prominent physicians is deliberate — they're who patients search
for, and a wrong paper on a famous surgeon's profile is the most damaging version
of the bug. But it means these pairs come from the *easy* end of the
distribution: median h-index 110, median 764 works, 192/200 with an ORCID.
A matcher scoring 97% here will do worse on a mid-career hospitalist named David
Kim with four papers. Treat the number as a ceiling and a regression tripwire.

**2. Specialty labels come from NPPES, not from the query that found the author.**
`specialty_bucket` is derived from the NPI's primary NUCC taxonomy;
`query_specialty` records which OpenAlex subfield query surfaced them. These
differ often, because prolific authors publish across many subfields — an early
run filed Paul Ridker under "Surgery" and Eric Topol under "Radiology" purely
because those queries reached them first. Stratified precision numbers must use
`specialty_bucket`.

## Coverage

200 pairs · 31 specialties · 27 states · mean confidence 0.879 (min 0.70)

Largest buckets: Allergy & Immunology, Anesthesiology, Cardiology, Critical Care,
Dermatology, Gastroenterology, Hematology (9 each). Thin buckets — Emergency
Medicine, Family Practice, Pediatrics, Radiology (2–3 each) — reflect a real
signal, not a bug: those specialties publish less per physician, so fewer of
their members clear the "well-published" filter. Expect matcher precision in
those specialties to be **least** well characterized by this set.

## How to spot-check

Every row carries the evidence needed to adjudicate it in seconds:

| Column | Use in review |
|---|---|
| `npi_url` | Opens the NPI registry record — confirm name, specialty, city |
| `openalex_url` | Opens the author's OpenAlex profile — confirm the papers fit that physician |
| `signals` | Which independent signals fired (`specialty`, `state`, `unique-name`, `middle-initial`, `orcid`, `md-do`, `h<N>`) |
| `nppes_namesakes` | How many NPI-1 providers share this exact first+last name. High = riskier |
| `runner_up_margin` | Gap to the second-best NPPES candidate. Low = the two were nearly tied |

Suggested procedure — roughly 30–40 minutes:

1. **Sample 30 rows, stratified**: 10 from `confidence >= 0.95`, 10 from
   `0.80–0.95`, 10 from `0.70–0.80`. Errors concentrate in the bottom band, so
   sampling uniformly at random would under-weight exactly where they live.
2. **Also review the 12 rows where `signals` contains `common-name` or
   `very-common-name`.** These are the structurally riskiest pairs in the file.
3. For each: open both URLs, confirm the person is the same human, write
   `match` / `no-match` / `unsure` into `verdict` and your name into `reviewer`.
4. If the bottom band is clean, raise nothing. If it isn't, raise
   `MIN_CONFIDENCE` in the script and regenerate — the HTTP cache makes a re-run
   take seconds rather than the ~45 minutes of the first run.

**One column not to trust during review:** `openalex_recent_institution` is
sometimes a mis-grouped affiliation rather than the physician's real employer
(Jeffrey Katz, a Brigham rheumatologist, shows "UT San Antonio"). This is the
known OpenAlex author-grouping caveat documented in
[publications.md](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/data-sources/publications.md).
Judge by the NPI record and the paper list, not by that column.

## Regenerating

```bash
python3 scripts/bootstrap_eval_set.py --target 200      # needs CONTACT_EMAIL only
python3 scripts/bootstrap_eval_set.py --no-cache        # ignore cached HTTP
```

Stdlib only, so it runs before `uv add`. Both APIs are free and unauthenticated;
responses cache under `$PEX_DATA_DIR/_cache/eval-bootstrap`. A cold run is ~2,000
HTTP calls and ~45 minutes, paced politely.

**Regenerating discards recorded verdicts** — the script rewrites the CSV. Copy
reviewed rows out first, or diff on the `(npi, openalex_author_id)` pair.

## What `held_ambiguous.csv` is for

Not rejects — *hard cases*. Pairs that were plausible but failed the confidence
bar or sat too close to a runner-up. Two later uses:

- **Recall testing.** The seed set can only measure precision on pairs the
  bootstrapper already found. The held file contains true matches it wasn't
  confident enough to keep, so a reviewed subset becomes a recall measure.
- **Threshold tuning.** These are the rows near the decision boundary, which is
  the only region where moving a threshold changes anything.
