# Decision 0001: Pull OpenAlex works from the bulk snapshot, not the API

**Status: proposed, needs sign-off.** This is the single largest timeline lever in the project — it changes bucket 3 from "2–5 months, or $150" to "a few hours, $0".

## The question

The [publications runbook](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/ingestion/publications.md) plans to pull works from the OpenAlex REST API, one paged walk per matched author, and prices it at **~$50–150 one-time nationally, or ~2–5 months** paced inside the free $1/day budget. It mentions the S3 snapshot in one line and dismisses it: *"pointless for works (666 GB)."*

The proposal on the table: **download a bulk works file once and join locally on author IDs**, avoiding per-author API calls entirely. The runbook's dismissal assumed you must move all 666 GB. You don't.

## What I measured

All figures below are from the live snapshot on 2026-08-15, not from documentation.

**The snapshot is published in Parquet as well as JSONL.** This is the fact the runbook's assessment missed, and it's the one that matters. Parquet is a columnar format: each column is stored in its own contiguous block, and a footer records where each block starts. That means a reader can fetch *only the columns it wants* — as HTTP range requests — instead of streaming whole records and discarding 90% of each one.

Snapshot inventory, read from the public manifests:

| Entity | Records | Parquet size | Files | Partitions |
|---|---:|---:|---:|---:|
| `works` | 510,372,821 | **725.0 GB** | 2,446 | 482 |
| `authors` | 119,129,660 | **52.8 GB** | 1,961 | — |

Snapshot date `2026-06-26`. Records are partitioned by `updated_date`, each partition holding the records that last changed on that date, in part files of up to 400,000 records.

**Access is anonymous.** No AWS account, no requester-pays, no key. The AWS Open Data program covers the egress. Both of these work:

```bash
aws s3 sync s3://openalex/data/parquet/works/ ./works/ --no-sign-request
curl https://openalex.s3.amazonaws.com/data/parquet/works/manifest.json      # plain HTTPS, no CLI
```

### The projection test

I took one representative works part file (98.3 MB, 84,037 records), then read it twice over HTTP range requests — once fully, once projected to just the columns this pipeline needs — and counted the bytes that actually crossed the wire:

```
remote file size: 98.3 MB

PROJECTED    rows=84,037  wire=  12.2 MB ( 12.4% of file)  range_reqs=24    2.0s
ALL columns  rows=84,037  wire=  98.2 MB ( 99.9% of file)  range_reqs=190  13.5s
```

**12.4%.** Projection genuinely reduces bytes transferred, not just bytes parsed. The reason is visible in the per-column breakdown:

| Column | Share of file |
|---|---:|
| `abstract_inverted_index` | **41.5%** |
| `concepts` (deprecated, superseded by `topics`) | 12.3% |
| `authorships` | 10.2% |
| `locations` (all copies of the work) | 5.1% |
| `title` + `display_name` (duplicates of each other) | 9.2% |
| `keywords`, `best_oa_location` | 7.2% |
| everything we actually need | **~12%** |

Two-fifths of the corpus is abstracts, which this product does not use. Another eighth is the deprecated `concepts` field. The columns bucket 3 needs — identity, title, year, journal, author links, author position, MeSH — are a thin slice.

### What that means at full scale

| | Runbook's API plan | Snapshot + projection |
|---|---|---|
| Author pool | 112k list calls ≈ $11, **~11 days** budget-paced | ~16 GB download, **~1 hour** |
| Works | ~$50–150, **2–5 months** free-paced (or ~1 week at $150) | ~90 GB download, **~3–4 hours** |
| OpenAlex spend | $11 – $161 | **$0** |
| Re-runnable? | No — re-walking costs the same again | Yes — it's a local file |

~90 GB is `725 GB × 12.4%`; ~16 GB is `52.8 GB × 30.2%` (measured the same way on an authors part file). Observed throughput was 14 MB/s on a full-file download and ~6 MB/s on the many-small-ranges projected read, so 106 GB total is a single overnight run at worst, and an afternoon at best.

## Recommendation

**Snapshot for the one-time backfill; API for ongoing freshness.** Neither alone is right:

- The **free snapshot is quarterly** (paid plans get daily). A quarterly-only pipeline means a physician's newest papers are up to three months stale — unacceptable for the product's core claim.
- The **API is priced per call**, which is ruinous for 510M records and trivial for the ~5,700 works that change in a given week.

So:

1. **Backfill from the snapshot**, projected. Filter authors to US-affiliated during the streaming pass, keep the matcher-input columns, write to `staging.openalex_author_pool`.
2. **Match locally** — unchanged from the runbook. Free, re-runnable, no API calls.
3. **Pull works from the snapshot**, projected, semi-joined against matched author IDs. `$0`.
4. **Weekly deltas from the API**, using `from_updated_date` for matched authors only. Cheap at that volume — this is what the free daily budget is actually well-suited to.
5. **Quarterly, re-sync the snapshot** as a backstop against missed deltas.

### The extra win: keep the authorship edge list

While making the one pass over works, also write a slim `(author_id, work_id, author_position)` edge list for **every** US-affiliated author, not just currently-matched ones. It's a few GB. The payoff is that when the matcher improves and newly matches 20,000 physicians, their works are **already local** — no new download, no new API spend. Without this, every matcher improvement re-opens the question of how to fetch works for the newly matched.

This is the part that turns the snapshot from a one-time shortcut into the durable design.

## Honest caveats

**1. OpenAlex's MeSH coverage is good but not complete — and the gaps are mostly a timing artifact.** Works carry a `mesh` column. Measured across 8 randomly sampled large partitions (2,670,076 works, 173,094 of them carrying a PMID):

- 6.5% of sampled works have a PMID (far higher inside medicine; corpus-wide includes all disciplines)
- **Of PMID-carrying works, 79.7% already have MeSH terms in the snapshot**

Coverage rises monotonically as papers age, which is the important part:

| Publication year | PMID works sampled | MeSH present |
|---|---:|---:|
| 2026 | 2,953 | 44.3% |
| 2025 | 7,980 | 54.0% |
| 2024 | 7,420 | 57.2% |
| 2022 | 8,759 | 63.2% |
| 2020 | 7,120 | 70.7% |
| 2018 | 4,585 | 71.5% |
| 2015 | 3,932 | 78.2% |
| all years | 173,094 | **79.7%** |

**MeSH is assigned by NLM indexers months after publication.** So an empty `mesh` array on a 2026 paper does not mean OpenAlex is missing data — it means *nobody* has MeSH for that paper yet, PubMed included. Calling `efetch` for it returns nothing useful.

This has a concrete design consequence: the PubMed step is not a one-time backfill but a backfill **plus a periodic re-check** as indexing catches up. See [03-publications.md](../prerequisites/03-publications.md#how-mesh-terms-actually-get-populated).

Also note the payload contains **duplicate descriptor entries** (the same `descriptor_ui` repeated within one work), so dedupe on load.

_An earlier revision of this doc reported 26% coverage. That figure came from a single `updated_date=2026-06-01` partition whose PMID works were 91% from publication year 2026 — the worst case, not the average. The 79.7% figure above supersedes it._

**2. Quarterly cadence for the free tier**, hence the hybrid above.

**3. Bandwidth, not money.** 106 GB is free but not instant, and it's a real consideration on a metered or capped connection.

**4. Disk.** ~106 GB projected, on top of ~11 GB of NPPES. Fine against 795 GB free, but this is the bucket that dominates the disk budget, and `staging.openalex_author_pool` should be archived to Parquet and dropped once match rates stabilize.

**5. Partitioned by `updated_date`, not by subject.** There is no "medicine only" or "US only" subset to download — the filter happens client-side during the streaming pass. So the *download* is 106 GB regardless of how narrow the launch vertical is; only the retained output shrinks.

## What this changes upstream

If accepted, the publications runbook needs revising: the "Exact source" table gains snapshot rows, the S3 line stops saying works are pointless, and the build timeline drops from "~2–5 months on the free budget, or ~1 week with a one-time ~$150 prepaid balance" to hours. That's a substantive edit to a docs-repo file, so it should be raised there rather than made unilaterally.

## Reproducing the measurements

The probe script is not committed — it was a throwaway. To re-verify:

```bash
# Inventory, from the public manifest — no credentials
curl -s https://openalex.s3.amazonaws.com/data/parquet/works/manifest.json \
  | python3 -c "import json,sys; m=json.load(sys.stdin); \
      print(m['date'], '{:,} records'.format(m['record_count']), \
            '{:.1f} GB'.format(m['content_length']/1e9), len(m['files']), 'files')"
```

For the projection measurement: fetch one part file, then use `pyarrow.parquet.ParquetFile(...).metadata.row_group(0).column(i).total_compressed_size` summed per `path_in_schema` to get the per-column breakdown, and a `RawIOBase` subclass issuing HTTP `Range` requests to count real wire bytes with and without `columns=`.
