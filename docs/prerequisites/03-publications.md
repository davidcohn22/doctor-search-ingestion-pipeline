# Prerequisites: Bucket 3 — Publications (OpenAlex + PubMed)

Everything that must be true before the publications pipeline runs. This is the largest, slowest, and highest-risk bucket — and the only one with an open decision that changes its cost by two orders of magnitude.

Runbook: [data/ingestion/publications.md](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/ingestion/publications.md)
Diagram: [diagrams/publications-ingestion-flow.md](https://github.com/davidcohn22/physician-expertise-docs/blob/main/diagrams/publications-ingestion-flow.md)

> **Blocked on a decision.** How works are fetched — bulk snapshot or REST API — is unresolved. See [decisions/0001](../decisions/0001-openalex-works-via-snapshot.md); the recommendation is snapshot-for-backfill plus API-for-deltas, which makes this bucket free and same-day rather than months-long. **The credential and download requirements below differ depending on which path is chosen**, so settle it before setting anything up.

## Credentials

| Variable | Path that needs it | How to get it |
|---|---|---|
| `NCBI_API_KEY` | Both | Free, instant. Sign in at [account.ncbi.nlm.nih.gov](https://account.ncbi.nlm.nih.gov/settings/) → API Key Management → Create. Raises E-utilities from 3 to 10 requests/sec. |
| `NCBI_EMAIL` | Both | Just your address — NCBI asks for it on every call and will throttle or block traffic without one. |
| `OPENALEX_API_KEY` | **API path only** | Free tier at [openalex.org/pricing](https://openalex.org/pricing). Carries a $1/day budget. |

**The snapshot path needs no OpenAlex credential and no AWS account.** The bucket is anonymously readable and the AWS Open Data program covers egress. If the snapshot recommendation is accepted, the only key this bucket needs is NCBI's.

Get the NCBI key first regardless — it's free, takes a minute, and it's needed by both paths.

## What gets downloaded — snapshot path (recommended)

| What | Size | Lands in | Mode |
|---|---:|---|---|
| OpenAlex `authors`, projected | **~16 GB** of 52.8 GB | `$PEX_DATA_DIR/openalex/authors/` | Bulk over anonymous HTTPS/S3 |
| OpenAlex `works`, projected | **~90 GB** of 725 GB | `$PEX_DATA_DIR/openalex/works/` | Bulk over anonymous HTTPS/S3 |
| OpenAlex reference sets (institutions, sources, topics) | <1 GB | `$PEX_DATA_DIR/openalex/` | Bulk |
| PubMed MeSH terms | n/a | — | **Pure API** — batched `efetch` |
| ORCID employment check | n/a | — | **Pure API** — only for candidates carrying an ORCID |

Snapshot layout (verified 2026-08-15, snapshot date `2026-06-26`):

```
s3://openalex/data/parquet/{works,authors,institutions,sources,topics}/
  manifest.json                                    # every file, with record_count + content_length
  updated_date=2026-06-26/part_0000.parquet        # ≤400k records per part file
```

Start from `manifest.json` rather than listing the bucket — it gives you every file path, its record count, and its byte length, so you can plan and resume a multi-hour download deterministically:

```bash
curl -s https://openalex.s3.amazonaws.com/data/parquet/works/manifest.json -o works_manifest.json
```

**Project the columns.** This is the whole trick — reading all columns moves 725 GB, reading the ones we need moves ~90 GB (measured: 12.4% of bytes). The needed set:

```
id, doi, ids, title, publication_year, publication_date, type, cited_by_count,
authorships.author.id, authorships.author.display_name, authorships.author_position,
authorships.institutions.id,
primary_location.source.id, primary_location.source.display_name,
primary_location.source.issn_l,
topics.id, topics.score,
mesh
```

Explicitly **not** fetched: `abstract_inverted_index` (41.5% of the corpus, unused), `concepts` (12.3%, deprecated in favour of `topics`), `locations`, `keywords`, `referenced_works`, `related_works`.

## What gets downloaded — API path (fallback)

| What | Cost | Time |
|---|---|---|
| US-author dump — `authors?filter=affiliations.institution.country_code:US` | ~112k list calls ≈ $11 | ~11 days budget-paced |
| Works per matched author — `works?filter=authorships.author.id:<id>` | ~$50–150 nationally | 2–5 months free, ~1 week at $150 |
| Reference dumps | <$1 | <1 day |

List calls cost $0.0001; `search=` calls cost $0.001. That 10× gap is why even the API path dumps and filters locally rather than searching per physician.

## Do this before the matcher: the eval set

**Build the ~200-pair eval set before tuning any matching logic.** [ADR 0001](https://github.com/davidcohn22/physician-expertise-docs/blob/main/architecture-decision-records/0001-v1-data-bucket-strategy.md) names this the project's first action, and it belongs in the prerequisites rather than the backlog.

The reason is asymmetric risk. No publication database contains NPIs, so linking a physician to an author is *probabilistic* — a scoring decision, not a lookup. A missing paper on a profile is a small gap a user will never notice. **A wrong paper on a profile is the most credibility-destroying bug this product can ship**: it tells a patient researching a surgeon that they published work they didn't. Without an eval set you have no error rate, only a feeling.

### Generate it, then spot-check it (2026-08-15)

Earlier drafts said "200 hand-verified pairs," which read as 200 manual lookups. That is unnecessary, and it is the same per-record manual labor ADR 0001's move 5 rules out elsewhere. The set is **bootstrapped and then sampled for review**:

```bash
python3 scripts/bootstrap_eval_set.py --target 200
```

**Why a generated set can still be trustworthy: it runs the lookup backwards.** The production matcher goes physician → candidate authors, which is the hard direction — one NPI, and potentially 1,160 OpenAlex authors named "Michael Smith." The bootstrapper goes the other way: it starts from *well-published US authors within a specialty* and resolves each to NPPES. That inverts the ambiguity. A prolific author with a distinctive name, a matching NPPES specialty, and a matching state resolves to exactly one NPI far more often than a randomly chosen physician resolves to one author. The script then keeps only pairs that clear a confidence bar **and** carry corroborating evidence beyond the name itself.

Each row ships with the signals that fired, the namesake count, the margin over the runner-up, and two click-through URLs (NPI registry, OpenAlex), so confirming or rejecting one takes seconds. Verdicts go in the `verdict` column. Details and the review procedure: [`eval/README.md`](../../eval/README.md).

**Two properties of this set are load-bearing, and both cut against overconfidence:**

- **It is not ground truth until it is sampled.** Until a human works through a sample and records verdicts, it is a *seed*, not a gold standard. Precision measured against an unreviewed seed is measuring the bootstrapper against itself.
- **Its precision is an upper bound, not the national rate.** The bias toward prominent, prolific physicians is deliberate — they're the ones patients search for, and they're the ones a wrong paper embarrasses most — but it means the set is drawn from the *easy* end of the distribution: distinctive publication records, stable affiliations, frequent ORCIDs. A matcher scoring 97% here will do worse on a mid-career hospitalist named David Kim. Treat the number as a ceiling and a regression tripwire, not a forecast.

The set is stored as a CSV in the repo (it's tiny and it's QA tooling, not profile data). It becomes the measuring stick every scoring change is tested against, and it's what makes the accept-threshold defensible instead of arbitrary.

This is internal tooling — it is **not** manual data entry for profiles, which [ADR 0001 explicitly ruled out](https://github.com/davidcohn22/physician-expertise-docs/blob/main/architecture-decision-records/0001-v1-data-bucket-strategy.md) as infeasible.

## Prerequisites checklist

1. **Bucket 1 loaded** — the matcher's input is the spine. ~1M physicians with names, alternate names, practice geography, and taxonomy codes.
2. **NCBI API key** in `.env`.
3. **The works-fetch decision made** ([0001](../decisions/0001-openalex-works-via-snapshot.md)).
4. **Launch specialties chosen** — determines matcher scope. Without them you can still build the author pool, which is specialty-agnostic.
5. **The ~200-pair eval set generated and spot-checked** — `python3 scripts/bootstrap_eval_set.py`, then review a sample (see [`eval/README.md`](../../eval/README.md)). Needs `CONTACT_EMAIL` set; no other credential.
6. **~110 GB free disk** (snapshot path).
7. **Reference sets loaded first** — institutions, sources, topics. The matcher's geography scoring resolves institution IDs to city/state through the institutions table, so it must exist before scoring runs.

## How MeSH terms actually get populated

MeSH descriptors are the condition-relevance signal everything downstream ranks on, so this is worth getting right rather than treating as a footnote.

**The snapshot already carries most of them.** Measured across 8 sampled partitions (173,094 PMID-carrying works), **79.7% already have MeSH terms** in OpenAlex's own `mesh` column — free, no API call. Do not blindly re-fetch what you already have.

**The gaps are mostly a timing artifact, not a data-quality gap.** NLM indexers assign MeSH months after publication, so coverage climbs as papers age: ~44% for 2026 papers, ~54% for 2025, ~71% for 2020, ~78% for 2015. An empty `mesh` array on a brand-new paper means **nobody** has MeSH for it yet — PubMed included — so calling `efetch` for it returns nothing useful.

That gives a three-part design:

| Step | What it covers | Cost |
|---|---|---|
| 1. Take `mesh` from the snapshot | ~80% of PMID works | $0, no calls |
| 2. Batched `efetch` backfill for the gap | Most of the remaining ~20% | minutes |
| 3. Periodic re-check sweep | Newly indexed recent papers | minutes/month |

### Step 2: batch, don't loop per work

**Never one call per work.** `efetch` accepts up to ~200 PMIDs per request, and with a key you get 10 requests/sec — so the batched volume is trivial:

```
Only works that (a) have a PMID and (b) have no MeSH from the snapshot need a call.

Launch vertical, ~30k matched physicians × ~25 works  ≈  750k works
  → ~85% with a PMID                                 ≈  640k
  → ~20% missing MeSH                                ≈  130k works
  → ÷ 200 per call                                   =   ~650 calls
  → ÷ 10 per sec                                     ≈   ~65 seconds

National, ~9M works  →  ~1.5M works needing MeSH  →  ~7,600 calls  ≈  13 minutes
```

Minutes, not months. This is nothing like the works-fetch problem that made the snapshot necessary — the gap here is small and targeted, so the API is the right tool.

### Step 3: the re-check sweep (not in the runbook)

Because MeSH arrives late, a work ingested at publication will never get MeSH unless something looks again. Track `mesh_checked_at` per work and re-check works that are PMID-carrying, still MeSH-less, and under ~2 years old. Without this, your newest papers — often the most relevant ones for judging current expertise — stay permanently unclassified, and the failure is silent.

Two ways to implement it, both fine:

- **Re-poll `efetch`** for the still-missing set, monthly. Simple, reuses step 2's code.
- **Ingest PubMed's daily update files** — `https://ftp.ncbi.nlm.nih.gov/pubmed/updatefiles/` (~257 files, ~19 MB each) — and upsert MeSH for any PMID you care about. No API calls at all, and newly indexed citations appear here as NLM publishes them.

### Why not the PubMed bulk baseline?

For completeness: NLM publishes all of MEDLINE as bulk XML at `https://ftp.ncbi.nlm.nih.gov/pubmed/baseline/` — **1,334 files, ~19 MB each, ~25 GB total**, verified 2026-08-15. It needs no API key and would give MeSH for everything in one pass.

It's the wrong trade *for MeSH alone*: 25 GB covering ~37M citations to fill a gap of ~130k–1.5M works, when the targeted API path takes minutes. The OpenAlex snapshot won because we needed a large fraction of that corpus; here we need a sliver.

It becomes the right call if either changes: if we ever want more from PubMed than MeSH (abstracts, grant/funding data, publication types, author affiliations), or if we want to remove the runtime API dependency entirely. Worth revisiting then, not now.

## API pacing

| Endpoint | Limit | Notes |
|---|---|---|
| PubMed `efetch` | **10 req/sec** with a key (3 without) | Batch up to ~200 PMIDs per call |
| ORCID public API | Undocumented; be polite | Called only when a candidate has an ORCID. **Cache results** — the same ORCID gets checked repeatedly across matcher re-runs |
| OpenAlex API | $1/day free budget | Snapshot path avoids this for the backfill |

Send a real `User-Agent` including `CONTACT_EMAIL` on every request. NCBI in particular treats anonymous bulk traffic as abuse.

## Resumability is not optional here

This is the one bucket where a job can run for hours, so state must survive a crash. `staging.ingest_state` holds one row per long-running job with its cursor and last-run time.

- **Snapshot path:** record each completed part file. A resumed run skips what's already local — the manifest's `content_length` lets you verify a file is complete rather than truncated.
- **API path:** persist the cursor after *every* batch. OpenAlex's cursor paging returns an opaque `next_cursor` passed verbatim to the next call; `null` means done. Unlike `page=N` offset paging, cursors are deterministic and non-overlapping, and don't cap at 10k records.

A multi-hour job that restarts from zero on a dropped connection will never finish. Test the resume path deliberately — kill the job halfway and restart it — before trusting an overnight run.

## Sanity checks

**Author pool:**

| Check | Expected |
|---|---|
| Rows in `staging.openalex_author_pool` | ~11M US-affiliated |
| Rows with a non-empty name-variant list | Majority |
| Rows with an ORCID | Minority — expected, ORCID coverage is partial |
| Normalized-name index exists | Yes — candidate lookup is unusably slow without it |

**Matching** — the numbers that matter, measured against the eval set. Quote these only once the set has been spot-checked, and quote precision as a **ceiling** — the set skews to well-published physicians ([`eval/README.md`](../../eval/README.md)):

| Check | Target |
|---|---|
| Precision at the accept threshold (≥0.90) | **As high as possible — this is the one to optimize** |
| Recall | Secondary. A missing paper is acceptable; a wrong one is not |
| Physicians with ≥1 accepted author link | Report it; it's a new baseline |
| Held-out review queue (0.60–0.90) | Non-empty. If it's empty, the threshold logic isn't splitting ambiguous cases — those should never be silently guessed |

**Works:**

| Check | Expected |
|---|---|
| `authorship.author_position` populated | Always — it's scoring input, since first/last authorship carries different weight in medicine |
| Works with a PMID | ~1.5% overall; far higher within medicine |
| MeSH from the snapshot, before any API call | **~80% of PMID-carrying works** — verify you're keeping these rather than re-fetching |
| MeSH coverage on works published this year | Low (~44%) and **expected** — NLM hasn't indexed them yet |
| Works still MeSH-less after the backfill | Should be almost entirely recent papers. Older gaps mean the batching or the PMID join is broken |
| `mesh_checked_at` populated | Required for the re-check sweep to work |
| Duplicate MeSH descriptors within one work | **Expected — dedupe on load.** OpenAlex repeats the same `descriptor_ui` |

```sql
ANALYZE work;
ANALYZE authorship;
ANALYZE physician_author;
```

## Refresh

| When | What |
|---|---|
| Weekly | API delta — re-walk works for matched authors using `from_updated_date` |
| Monthly | **MeSH re-check sweep** — PMID-carrying, still MeSH-less, under ~2 years old ([why](#step-3-the-re-check-sweep-not-in-the-runbook)) |
| Quarterly | Re-sync the snapshot (free tier publishes quarterly); run the per-physician `search=` fallback for still-unmatched physicians |
| On any scoring change | Re-run the matcher — free and local, upserts by `(npi, openalex_id)` so links update in place |
| Once match rates stabilize | Archive `staging.openalex_author_pool` to Parquet and drop it (~8 GB reclaimed) |

## Related

- [decisions/0001](../decisions/0001-openalex-works-via-snapshot.md) — the snapshot-vs-API analysis and measurements
- [Runbook](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/ingestion/publications.md) — cursor walk, slimming, matcher structure
- [05-condition-vocabulary.md](05-condition-vocabulary.md) — depends on works being loaded before it can prune
