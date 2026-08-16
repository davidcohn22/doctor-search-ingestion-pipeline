# Decision 0002: Implementation choices made while building the first pipeline draft

**Status: provisional.** Every choice below was an open question in the docs when the first
implementation pass was written. Each was resolved with the most defensible default so the code
could exist and run; each is cheap to change. This file is the record of what was assumed and why,
so nothing is silently baked in.

Where a choice contradicts or extends something in the docs repo, it is flagged as needing
reconciliation there.

## 1. Adopt decision 0001 — snapshot backfill, API deltas

[Decision 0001](0001-openalex-works-via-snapshot.md) is marked "proposed, needs sign-off". The code
implements its recommendation: bulk snapshot with column projection for the one-time backfill, REST
API `from_updated_date` deltas for ongoing freshness.

**Why assume yes.** It is the difference between a bucket that takes an afternoon and one that takes
months, the analysis is measurement-backed, and the fallback is not lost — `deltas.py` already
contains a working cursor walk, so switching to the pure-API path is a change of caller, not a
rewrite.

The durable half of that decision — the `(author_id, work_id, position)` edge list for *every*
US-affiliated author — is implemented in `works.py` and backed by `staging.openalex_authorship_edge`.
Without it, every future matcher improvement re-opens the question of how to fetch works for the
newly matched.

## 2. `work_mesh`: canonical JSON, derived expansion

The docs record an unresolved inconsistency — runbooks write to a `work_mesh` link table, the schema
stores a JSON array on `work.mesh_terms`
([ddl/README.md](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/database/ddl/README.md#known-inconsistency-work_mesh)).

**Resolved as the third option the DDL README leaves open.** `work.mesh_terms` stays canonical and
unchanged; `staging.work_mesh_expanded` is a derived expansion rebuilt by the prune step, and the
three counting queries (pruning, specialty mapping, feature build) join against it. Nothing is
maintained in two places. Full reasoning in [migrations/README.md](../../migrations/README.md).

**If the expansion later proves expensive enough to want incrementally maintained**, that is the
moment to promote `work_mesh` into the canonical schema — the staging table is already its shape.

## 3. `work.mesh_checked_at` — a new column on a serving table

**Needs reconciling into the docs repo.** The MeSH re-check sweep is required by
[03-publications.md](../prerequisites/03-publications.md#step-3-the-re-check-sweep-not-in-the-runbook),
and its sanity checks list "`mesh_checked_at` populated" as a required outcome — but the DDL has no
such column. Added in `migrations/0001`. Without it the sweep cannot tell "PubMed has no MeSH for
this paper yet" from "we never asked", and the newest papers stay permanently unclassified.

## 4. Staging holds the filtered NPPES subset, not a faithful 8.5M-row copy

The runbook says staging should be an unfiltered copy so filter logic can change without
re-downloading 10 GB. The loader instead stages the filtered, projected physician subset
(~1M rows, ~60 columns).

**The property the runbook wants is preserved by the dated snapshot directory**, which keeps the
untouched CSV on disk permanently — re-filtering re-reads a local file and downloads nothing. What
this avoids is carrying ~10 GB of mostly-empty columns for provider types we will never serve inside
Postgres, where the disk budget is tightest. Stated in the module docstring at the point it matters
(`buckets/nppes/stage.py`).

## 5. Condition vocabulary: tight scope, Opus 5, relative pruning

| Question | Choice | Reasoning |
|---|---|---|
| Tree scope | **C + F03 + E04** (~6,200 descriptors) | The prerequisites doc's own recommendation: start tight. E01/E02 can be added later by re-running those branches only. |
| Exclusions | **F01, F02, F04, D**, explicitly | Taking all of `F` adds ~1,000 psychological-phenomena descriptors that look like conditions to a prefix filter but aren't. |
| Model | **`claude-opus-5`** via the Batches API | The runbook says `claude-sonnet-5`; the prerequisites doc says pick on output quality, not cost, since this becomes user-visible search vocabulary. The gap is ~$7 vs ~$4 for the entire national job. `--live --limit 50` exists to run the comparison before committing. |
| Pruning | **top-N per branch, with the absolute count as a floor** | The runbook's ≥25 was sized for one narrow vertical; nationally it stops filtering. A relative rule also self-corrects as the corpus grows. |
| `branch_config.yaml` | **National form** — branch include/exclude lists, no per-specialty stanzas | Per the prerequisites doc: "all specialties" makes the per-specialty version unwritable. |
| `condition_taxonomy` | **Derived from who publishes**, not authored | The inverted mapping the prerequisites doc calls for. Implemented in `taxonomy_map.py`. |

## 6. Matcher weights are a starting hypothesis, not a tuned model

The *structure* follows the runbook and is meant to be stable: independent signals, accept at ≥0.90,
a held band at 0.60–0.90 that is never guessed at, a margin test so a near-tie is held rather than
resolved by a coin flip, and ORCID employment confirmation flooring a pair at 0.97.

The *weights* are invented. They must be tuned against `eval/physician_author_pairs.csv`, optimizing
precision with recall explicitly secondary. **No precision number has been measured yet**, because
nothing has been run against real data.

Two structural choices worth noting:

- **A physician may accept several author identities** (OpenAlex fragments one human across
  entities, and the composite primary key exists to re-unite them), but a *trial mention* accepts
  exactly one physician. Both use the same `resolve()`; the trials caller passes `multi_accept=False`.
- **`rejected` is never written by the matcher.** It is reserved for human verdicts, so a refresh
  cannot re-propose a ruled-out candidate — and both upserts have a `WHERE status <> 'rejected'`
  guard so a re-run cannot overwrite one.

## 7. Additions the docs imply but never name

| Thing | Where | Why it had to exist |
|---|---|---|
| `staging.physician_name_key`, `staging.physician_match_facts` | migration 0004 | Candidate retrieval needs a name key including alternate names. `physician.search_name` is a generated column and cannot expand a JSON array — a TODO the DDL README already flags. |
| `staging.geocode_cache`, `staging.zip_centroid` | migration 0005 | "Only geocode new or address-changed physicians" needs somewhere to remember what was geocoded, and the documented ZIP-centroid fallback needs a centroid source (Census Gazetteer ZCTA file — free, no key). |
| `staging.openalex_institution/topic/source` | migration 0002 | The reference sets the matcher's geography and specialty signals resolve through. |
| `staging.orcid_employment_cache` | migration 0006 | Required by the prerequisites doc's "cache results" instruction; negatives are cached too, since the matcher is designed to be re-run freely. |

## 8. Deliberately not built

- **`splink`** — the runbook names it as the upgrade path if hand-tuned weights plateau against the
  eval set, not a day-one dependency. Hand-tuned weights first, measured, then decide.
- **The PubMed bulk baseline** (~25 GB of MEDLINE XML). The targeted `efetch` backfill covers the
  MeSH gap in minutes. Worth revisiting only if we ever want more than MeSH from PubMed, or want to
  remove the runtime API dependency entirely.
- **Secondary practice addresses** (`pl_pfile_*.csv`), v2 profile buckets, and anything else the
  docs mark v2. The v2 *columns* on v1 tables are left null/false as designed.
- **A scheduler.** Phase 1 is `launchd` (see [deploy/launchd/](../../deploy/launchd/README.md)),
  phase 2 is GitHub Actions cron. Config comes from the environment precisely so that move is a
  configuration change.

## What has and hasn't been verified

**Verified:** every module imports and compiles; 68 unit tests pass over the pure logic — name
normalization, matcher thresholds and margins, NPPES header resolution across both layouts, trial
slimming, MeSH branch selection, and LLM reply parsing.

**Not verified:** nothing has been run against a live source or a real database. No download, no
API call, no SQL executed. Column names in the NPPES V2 layout, the exact shape of the OpenAlex
snapshot manifest, and the MedlinePlus XML element names are all handled defensively (resolved at
runtime, with loud failures on mismatch) but have not been confirmed against the real files.

Two unit tests exist specifically because they encode bugs found while writing this draft: periods
being split before credential detection (which turned "Jane Roe, M.D." into a doctor named "M D"),
and dot-boundary prefix matching rejecting every top-level MeSH branch (`C` never matching
`C12.294`). Both would have been silent in production.
