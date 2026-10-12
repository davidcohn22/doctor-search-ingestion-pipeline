# doctor-search-ingestion-pipeline

Python data-ingestion pipeline for Physician Expertise Explorer. Pulls physician identity, education, publications, clinical trials, and condition vocabulary from public sources into PostgreSQL, which is the contract between this repo and the serving application.

Product, architecture, schema, and per-source runbooks live in the companion docs repo, [physician-expertise-docs](https://github.com/davidcohn22/physician-expertise-docs). This repo holds the code and the operational setup.

## Status

**First implementation draft.** All five buckets, the feature build, and the CLI are written and unit-tested. **Nothing has been run against a live source or a real database yet** — no download, no API call, no SQL executed. Treat the code as a well-specified starting point, not a proven pipeline.

What that means concretely:

- The pure logic is tested (68 unit tests): name normalization, matcher thresholds and margins, NPPES header resolution across both file layouts, trial slimming, MeSH branch selection, LLM reply parsing.
- Everything that touches a source is written defensively — headers and URLs are resolved at runtime, mismatches fail loudly — but the real files have not been seen.
- The matcher's *structure* follows the runbooks; its *weights* are an untuned starting hypothesis. No precision number exists yet. Tune against [`eval/`](eval/README.md) before trusting a single accepted match.
- Every open question resolved by assumption is recorded in [decisions/0002](docs/decisions/0002-implementation-choices.md).

## Quick start

```bash
brew install uv
cp .env.example .env && $EDITOR .env      # DATABASE_URL, PEX_DATA_DIR, CONTACT_EMAIL
git check-ignore -v .env                  # confirm it's ignored BEFORE adding secrets
mkdir -p ~/pex-data/{nppes,cms-dac,openalex/{authors,works},ctgov,mesh,nucc,geocode,_archive}
uv sync

# Load the canonical schema from the docs repo, then this repo's additions.
psql -d pex -f ../physician-expertise-docs/data/database/ddl/run-all.sql
psql -d pex -f ../physician-expertise-docs/data/database/ddl/08-roles-and-grants.sql   # edit passwords first
DATABASE_URL=postgresql://$USER@localhost:5432/pex uv run pex migrate   # as the schema owner

uv run pex preflight                      # checks everything above in one command
```

Full walkthrough: [docs/prerequisites/00-workstation.md](docs/prerequisites/00-workstation.md).

## Running it

```bash
uv run pex run nppes --seed          # bucket 1 — the physician spine (~1 hour)
uv run pex run cms                   # bucket 2 — medical school (minutes)
uv run pex run publications --all    # bucket 3 — the long pole (hours)
uv run pex run trials --seed         # bucket 4 — whole world corpus (~13 min)
uv run pex vocab list|merge|cards|assemble|check|review|load --run R   # bucket 5 — concept vocabulary
uv run pex features                  # rebuild the tiered search features — after EVERY load
uv run pex status                    # what has run, when, and what it did
```

**Bucket 1 must load before any other** — the matchers in 3 and 4 read the spine as their input, and bucket 2 joins onto it. Buckets 2, 3 and 4 are independent of each other. Bucket 5 finalizes after 3, because its pruning step and its specialty mapping both need works in the database. See [run order](docs/prerequisites/README.md#run-order).

Every bucket has finer-grained steps (`pex run publications --help`), because the expensive ones are worth running and inspecting a stage at a time.

**Paper attribution** (2026-10-11), in this order after any matcher change: `--pool` (if the snapshot changed) → `--orcid-fetch` / `--orcid-dump` → `--match --workers 8` → `--orcid-apply` → `--remove-demoted` → `--works --no-edges --no-resume --workers 6` → `--paper-filter` → `--remove-demoted` → `pex features`. See the publications runbook in the docs repo for what each step fixes.

**Concept vocabulary** (replaced `pex run conditions` on 2026-10-11): generated per specialty from `config/specialty_groups.yaml`, checked against the literature, loaded into the `condition*` tables. See `src/pex_ingest/vocabulary/job.py` and the docs repo's `data/ingestion/condition-vocabulary.md`.

## The five v1 buckets

| # | Bucket | Source | Credential | Cost |
|---|---|---|---|---|
| 1 | Identity spine | NPPES bulk + Census geocoder | none | $0 |
| 2 | Medical school | CMS Doctors & Clinicians | none | $0 |
| 3 | Publications | OpenAlex snapshot + PubMed + ORCID | NCBI key (free) | $0 via bulk snapshot |
| 4 | Clinical trials | ClinicalTrials.gov v2 API | none | $0 |
| 5 | Concept vocabulary | Claude (Sonnet 5.5 lists, Haiku 5.5 cards) + our paper corpus | Anthropic key | ~$8 per full run |

## Layout

```
src/pex_ingest/
├── cli.py             # `pex` — one entry point for every job
├── config.py          # settings from the environment, never hardcoded
├── db.py              # connections, batched upserts, COPY, ANALYZE
├── http.py            # one polite client: pacing, retry, caching, User-Agent
├── state.py           # staging.ingest_state — resumable walks and run history
├── runs.py            # run summaries: rows in / written / rejected / elapsed
├── names.py           # ONE definition of "same name", shared by buckets 3 and 4
├── matching.py        # shared scoring, thresholds, accept/hold/drop decisions
├── specialties.py     # NUCC taxonomy text ↔ OpenAlex subfields
├── spine.py           # the matcher's view of the physician spine
├── buckets/
│   ├── nppes/         # 1 — sources, columns, stage, transform, geocode, taxonomy
│   ├── cms_dac/       # 2
│   ├── publications/  # 3 — snapshot, reference, author_pool, matcher, works, mesh, orcid, deltas
│   ├── trials/        # 4 — api, slim, matcher
│   └── conditions/    # 5 — branch_config, mesh_parse, medlineplus, prune, enrich, load, qa
└── features/          # the precomputed search features
config/branch_config.yaml   # the one file a human authors
migrations/                 # additive SQL on top of the docs-repo DDL
deploy/launchd/             # phase 1 scheduling
tests/                      # unit tests over the pure logic
```

## Ground rules

- **Raw files never live in this repo.** They land in `$PEX_DATA_DIR`, outside both git repos, in dated snapshot directories that are never overwritten.
- **Every write is an upsert**, so every job is safe to re-run — the basis for unattended scheduling.
- **Every long walk is resumable** via `staging.ingest_state`. Test the resume path deliberately before trusting an overnight run.
- **Config comes from the environment**, never hardcoded — this is what makes the move to hosted Postgres a config change rather than a rewrite.
- **Every job logs a run summary.** A silent failure looks exactly like success; `pex status` is how you tell them apart.
- **A missing paper is a small gap; a wrong paper is the worst bug this product can ship.** Ambiguous matches are held, never guessed.

## Related

- [physician-expertise-docs](https://github.com/davidcohn22/physician-expertise-docs) — vision, ADRs, schema, ingestion runbooks
- [docs/prerequisites/](docs/prerequisites/README.md) — what must be true before any job runs
- [docs/decisions/0001](docs/decisions/0001-openalex-works-via-snapshot.md) — OpenAlex snapshot vs API
- [docs/decisions/0002](docs/decisions/0002-implementation-choices.md) — what this draft assumed, and what still needs confirming
- [migrations/README.md](migrations/README.md) — the schema additions this pipeline needs, and how `work_mesh` was resolved
