# Ingestion Prerequisites

What must exist — installed, downloaded, registered, configured — **before** any v1 ingestion job runs. One file per bucket, plus the shared setup every bucket depends on.

These docs are the operational companion to the [ingestion runbooks](https://github.com/davidcohn22/physician-expertise-docs/tree/main/data/ingestion) in the docs repo. The runbooks answer *how the data is transformed*; these answer *what has to be true before you can run them at all*.

_All endpoints, file names, and sizes in this folder were verified live on 2026-08-15. Re-verify before a first run — government file names embed dates and change monthly._

## Read these first

| Doc | Covers |
|---|---|
| [00-workstation.md](00-workstation.md) | Everything installed once: Python, Postgres, DuckDB, the data directory, secrets |
| [01-nppes-identity.md](01-nppes-identity.md) | Bucket 1 — the physician spine |
| [02-cms-medical-school.md](02-cms-medical-school.md) | Bucket 2 — medical school + graduation year |
| [03-publications.md](03-publications.md) | Bucket 3 — OpenAlex + PubMed |
| [04-clinical-trials.md](04-clinical-trials.md) | Bucket 4 — ClinicalTrials.gov |
| [05-condition-vocabulary.md](05-condition-vocabulary.md) | Bucket 5 — MeSH + MedlinePlus + LLM enrichment |

One decision is still open and it affects bucket 3's timeline and cost: [decisions/0001-openalex-works-via-snapshot.md](../decisions/0001-openalex-works-via-snapshot.md).

## The one-screen summary

Nothing here requires a purchase, a contract, or an approval queue. Four of the five buckets need **no credentials at all**. The total setup is: install a current Python, create a data directory, register two free API keys, and have a working `pex` database.

| Bucket | Credential needed | Cost | Blocked on a decision? |
|---|---|---|---|
| 1. NPPES identity | none | $0 | No |
| 2. CMS medical school | none | $0 | No |
| 3. Publications | NCBI key (free), OpenAlex key (free tier) | $0 via snapshot | **Yes** — snapshot vs API |
| 4. Clinical trials | none | $0 | No |
| 5. Condition vocabulary | Anthropic API key | **≤ ~$8** national, all specialties | No — scope decided |

**Launch scope is national, all specialties** (supersedes the narrowed-vertical assumption in ADR 0001). Total money cost across all five buckets is the ≤ ~$8 Anthropic spend in bucket 5; everything else is free. The binding constraints are download time, local compute for matching, and human QA review — not dollars.

## Where files live

Raw downloads land **outside both git repos**, in a tree pointed at by the `PEX_DATA_DIR` environment variable. Nothing in this repo ever contains a data file.

```
~/pex-data/                          # $PEX_DATA_DIR
├── nppes/
│   ├── 2026-08-full/                # monthly full file, one dir per release
│   └── 2026-08-03_2026-08-09/       # weekly incremental, one dir per week
├── cms-dac/2026-07/                 # CMS Doctors & Clinicians national file
├── openalex/
│   ├── authors/                     # projected parquet from the snapshot
│   └── works/
├── ctgov/2026-08/                   # ClinicalTrials.gov JSONL cache
├── mesh/2026/                       # desc2026.gz + MedlinePlus topics
├── nucc/                            # taxonomy reference CSV
├── geocode/                         # Census batch request/response CSVs
└── _archive/                        # parquet keepsakes after a bucket is loaded
```

Three rules make this work:

1. **Dated snapshot directories, never overwritten.** A re-download of the August NPPES file goes in `nppes/2026-08-full/`; September's gets its own directory. When a parse changes, you re-read the file you already have instead of re-downloading 10 GB, and you can always diff two releases to see what actually changed.
2. **Raw files are disposable.** Every one is re-downloadable, and the database is the system of record. Deleting `~/pex-data` costs you download time, never data.
3. **The path is never hardcoded.** Every job reads `PEX_DATA_DIR` from the environment. That single indirection is what lets the pipeline later run on a server, or against an external drive, with no code change.

### Why outside the repo

A git-ignored `data/` folder inside the repo would work, but it fails in three specific ways this layout avoids: a `git add -A` before the ignore rule is right commits a gigabyte-scale file into history permanently; a fresh clone silently starts with an empty data directory and every job fails at the first read; and the repo directory becomes something you can't casually move or re-clone. Keeping data outside means the repo stays a few megabytes of code forever.

### Disk budget

Roughly **130 GB** at peak, before Postgres. Measured sizes, not estimates:

| What | Compressed | On disk after extract |
|---|---:|---:|
| NPPES monthly full | 1.07 GB | ~10 GB |
| NPPES weekly incremental | 11 MB | ~100 MB |
| CMS Doctors & Clinicians | — | 0.78 GB |
| OpenAlex authors (projected) | ~16 GB | ~16 GB |
| OpenAlex works (projected) | ~90 GB | ~90 GB |
| ClinicalTrials.gov corpus | — | ~2 GB |
| MeSH + MedlinePlus + NUCC | 16 MB | ~350 MB |

Postgres itself needs a few GB for a narrowed launch vertical, up to ~25–40 GB national. You have ~795 GB free, so no external drive is needed — but the two OpenAlex directories are the ones to prune first (`_archive/` as parquet, then delete) once the matcher stabilizes.

## Credentials and where they go

Every secret lives in a single git-ignored `.env` at the repo root, loaded by `python-dotenv`. Copy [`.env.example`](../../.env.example) to `.env` and fill it in. **Never commit `.env`** — [`.gitignore`](../../.gitignore) already excludes it, but the habit matters more than the rule.

| Variable | Needed by | How to get it | Notes |
|---|---|---|---|
| `DATABASE_URL` | all buckets | You set it — see [00-workstation.md](00-workstation.md) | `postgresql://pex_ingest:PASSWORD@localhost:5432/pex` |
| `PEX_DATA_DIR` | all buckets | You set it | Absolute path, e.g. `/Users/dcohn/pex-data` |
| `CONTACT_EMAIL` | all buckets | Yours | Sent as the HTTP `User-Agent` — several agencies ask for it and rate-limit anonymous traffic harder |
| `NCBI_API_KEY` | 3 | Free, instant — [account settings](https://account.ncbi.nlm.nih.gov/settings/) | Raises PubMed from 3 to 10 requests/sec |
| `NCBI_EMAIL` | 3 | Yours | NCBI asks for it on every E-utilities call |
| `OPENALEX_API_KEY` | 3 | Free tier — [openalex.org/pricing](https://openalex.org/pricing) | Only needed for the API path; the snapshot needs nothing |
| `ANTHROPIC_API_KEY` | 5 | [platform.claude.com](https://platform.claude.com) → API keys | The only bucket that spends money |

Two things deliberately absent: **no AWS account** (the OpenAlex snapshot is anonymous-readable — see bucket 3) and **no Census API key** (the geocoder is unauthenticated).

## Run order

Later buckets depend on earlier ones being in the database. The dependency is real, not stylistic — the matchers in buckets 3 and 4 read the spine as their input.

```
   ┌─ 0. Database exists (schema loaded, roles created)
   │
   ├─ 1. NPPES spine ──────────────┬──────────────┐
   │      ~1M physicians           │              │
   │                               ▼              ▼
   ├─ 2. CMS medical school   3. Publications  4. Clinical trials
   │      NPI-keyed join           (long pole)     (fast)
   │                               │
   │                               ▼
   └─ 5. Condition vocabulary — parse anytime, but pruning needs bucket 3
```

- **1 before everything.** Nothing works without the physician spine; the matchers have nothing to match against.
- **2, 3, 4 are independent** of each other and can run in any order (or in parallel) once 1 is loaded.
- **5 splits in two.** The MeSH parse runs any time; the pruning step ("keep only conditions the literature actually writes about") needs works in the database, so the vocabulary *finalizes* after 3.

## Before you run anything: the preflight

Every bucket doc ends with its own verification block. These four checks are common to all of them and worth running once, first:

```bash
# 1. Python is current, not Apple's 3.9
python3 --version                      # want 3.12+ — see 00-workstation.md

# 2. Postgres is up and the schema is loaded
pg_isready                             # want: accepting connections
psql -d pex -c "\dt"                   # want: 16 serving tables
psql -d pex -c "\dt staging.*"         # want: 5 staging tables

# 3. The data directory exists and is writable
test -w "$PEX_DATA_DIR" && echo "PEX_DATA_DIR ok: $PEX_DATA_DIR"

# 4. Enough disk for the largest single bucket
df -h "$PEX_DATA_DIR" | tail -1        # want: 150 GB+ available
```

## Scheduling: how this becomes autonomous

The goal is frequent, unattended re-pulls. That arrives in two phases, and **phase 1 is written so phase 2 is a configuration change rather than a rewrite**.

**Phase 1 — now. Local, scheduled with `launchd`.** Jobs run on this Mac against local Postgres. This is the right place to start: bulk loads are fast against a local database, and there is no monthly bill while the pipeline is still changing shape every week. The limitation is honest — the laptop has to be awake and unlocked for a job to fire, so a missed window just runs late.

**Phase 2 — later. Hosted Postgres + GitHub Actions.** When the schema stops moving, provision managed Postgres (Neon/Supabase/RDS, ~$0–35/month) and move the schedules into GitHub Actions cron. Then jobs fire whether or not the laptop is on.

What phase 1 must get right for phase 2 to be cheap:

- **Config from the environment, never hardcoded.** `DATABASE_URL` and `PEX_DATA_DIR` are read, not embedded. This is the whole migration: `.env` becomes GitHub Actions secrets, and the connection string points somewhere else.
- **Every job idempotent.** Writes are `INSERT … ON CONFLICT … DO UPDATE`, so a re-run after a crash or a double-fire can never duplicate data. This is what makes unattended scheduling safe rather than nerve-wracking.
- **Every long walk resumable.** Paginated API positions persist to `staging.ingest_state` after each batch, so an interrupted multi-hour job continues instead of restarting.
- **Every job logs a run summary** — rows in, rows written, rows rejected, elapsed. Unattended jobs you can't inspect afterwards are worse than no schedule at all, because a silent failure looks exactly like success.
- **Downloaders are content-addressed.** Resolve the current file, compare against what's already in `PEX_DATA_DIR`, skip if unchanged. A weekly job that re-downloads 10 GB to discover nothing changed is how a schedule gets switched off.

Refresh cadences per bucket, once phase 1 is in place:

| Bucket | Cadence | Runtime |
|---|---|---|
| 1. NPPES | Weekly incremental; monthly full true-up | minutes / ~1 hour |
| 2. CMS medical school | Monthly | minutes |
| 3. Publications | Weekly delta; quarterly snapshot refresh | minutes / hours |
| 4. Clinical trials | Weekly delta; quarterly full re-pull | minutes |
| 5. Condition vocabulary | Annually (December MeSH release); re-prune after each publication load | minutes |

## Conventions every bucket follows

- **Bulk files land in `staging` first**, then a transform writes clean rows to the serving tables. A parsing improvement is then a local re-run, never a re-download.
- **Every write is an upsert**, keyed on the source's own identifier.
- **Every product row carries provenance** — `source`, `source_record_id`, `confidence`, `retrieved_at`, `raw`.
- **API pacing is polite by default** — stay inside documented rate limits, send a real `User-Agent` with `CONTACT_EMAIL`, and persist pagination state.
- **`ANALYZE` after every bulk load.** Postgres does not update planner statistics automatically after a large insert, and the first query against a freshly loaded table can be pathologically slow without it.

## Open questions

- **OpenAlex works: snapshot or API?** See [decisions/0001](../decisions/0001-openalex-works-via-snapshot.md). The single largest timeline lever in the project.
- **`condition_taxonomy` mapping under national scope.** `branch_config.yaml` was specified as a hand-written specialty → MeSH-branch map, which doesn't scale to all specialties. The mapping has to invert — derive it from which specialties' matched physicians publish on each descriptor. See [05-condition-vocabulary.md](05-condition-vocabulary.md#the-real-cost-of-going-national-the-design-not-the-bill). This is a real change to the runbook's step 5.
- **Pruning threshold at national scale.** The suggested ≥25 papers was sized for one narrow vertical; nationally it may stop filtering. Likely wants a relative threshold (top-N per branch).
- **Matching eval set composition.** *Partly resolved 2026-08-15.* The set is now generated rather than hand-curated ([`scripts/bootstrap_eval_set.py`](../../scripts/bootstrap_eval_set.py) → [`eval/`](../../eval/README.md)), and stratification is handled: 200 pairs spread over 31 NPPES specialties and 27 states, labeled by the physician's actual taxonomy. Two things remain open. **(a)** The set skews to well-published physicians by design, so its precision is an upper bound — the national rate needs a second, deliberately *unremarkable* sample to characterize, and building that one is harder because low-publication physicians are exactly the ambiguous cases. **(b)** Thin specialties (Emergency Medicine, Family Practice, Pediatrics, Radiology: 2–3 pairs each) are too small to support a per-specialty precision number; those buckets need targeted topping-up before anyone quotes a stratified figure.
- **`work_mesh` table vs `work.mesh_terms` JSON column** — unresolved in the schema docs; bucket 5's pruning query is written differently depending on the answer.

Now resolved: **launch specialties.** Scope is national with all specialties, so buckets 1–4 need no specialty decision and bucket 5's blocker is replaced by the mapping question above.
