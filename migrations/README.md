# Migrations

Additive SQL this pipeline needs **on top of** the canonical v1 schema in the docs repo
([`data/database/ddl/`](https://github.com/davidcohn22/physician-expertise-docs/tree/main/data/database/ddl)).

Apply them after the DDL, with `pex migrate`:

```bash
psql -d pex -f /path/to/physician-expertise-docs/data/database/ddl/run-all.sql
psql -d pex -f /path/to/physician-expertise-docs/data/database/ddl/08-roles-and-grants.sql
pex migrate
```

Each file runs once and is recorded in `staging.schema_migration`, so re-running is a no-op.

## The rule these follow

The docs-repo DDL is the contract between this pipeline and the .NET serving application
([ADR 0003](https://github.com/davidcohn22/physician-expertise-docs/blob/main/architecture-decision-records/0003-implementation-stack.md)),
and it is deliberately not a migration system. So:

- **Anything in `staging` can be added here freely.** It is ingestion scaffolding; nothing the
  application reads depends on it, and `pex_ingest` already holds `CREATE` on that schema.
- **Anything touching a `public` table must be reconciled back into the docs repo.** There is
  exactly one such change right now (`work.mesh_checked_at`, migration 0001) and it is flagged
  below as needing that reconciliation.

## What each migration adds, and why the DDL doesn't already have it

| File | Adds | Why |
|---|---|---|
| `0001_work_mesh_checked_at.sql` | `work.mesh_checked_at` | **Touches a serving table — needs reconciling into the docs repo.** [03-publications.md](../docs/prerequisites/03-publications.md#step-3-the-re-check-sweep-not-in-the-runbook) requires tracking when a work was last checked for MeSH, and its sanity checks list "`mesh_checked_at` populated" as a required outcome — but no column exists. Without it the monthly re-check sweep cannot tell "PubMed has no MeSH for this yet" from "we never looked", and the newest papers stay permanently unclassified. |
| `0002_openalex_reference.sql` | `staging.openalex_institution`, `staging.openalex_topic`, `staging.openalex_source` | The matcher's geography scoring resolves institution IDs to city/state, and its specialty scoring resolves topic IDs to subfields. [03-publications.md](../docs/prerequisites/03-publications.md) requires the reference sets be loaded before matching; the DDL has nowhere to put them. |
| `0003_openalex_edges.sql` | `staging.openalex_authorship_edge` | The durable edge list from [decision 0001](../docs/decisions/0001-openalex-works-via-snapshot.md#the-extra-win-keep-the-authorship-edge-list) — `(author_id, work_id, position)` for every US-affiliated author, so a matcher improvement that newly matches 20,000 physicians finds their works already local instead of re-downloading 90 GB. |
| `0004_matcher_spine_index.sql` | `staging.physician_name_key`, `staging.physician_match_facts` | Candidate retrieval joins on a normalized name key including NPPES alternate names. `physician` has no such column (and can't: `search_name` is a generated column and Postgres cannot expand a JSON array inside one — see the DDL README). Both matchers share these. |
| `0005_geocode_cache.sql` | `staging.geocode_cache`, `staging.zip_centroid` | Geocoding is cached per *address*, not per physician, so a 200-doctor clinic costs one Census lookup and an unchanged address is never re-sent. `zip_centroid` backs the documented ZIP-centroid fallback for addresses the geocoder can't match — the DDL has the `practice_geo_precision` flag but nothing to compute `'zip_centroid'` from. |
| `0006_condition_pipeline.sql` | `staging.work_mesh_expanded`, `staging.mesh_coverage`, `staging.mesh_paper_count`, `staging.orcid_employment_cache` | See below for the first three. The ORCID cache is required by [03-publications.md](../docs/prerequisites/03-publications.md#api-pacing) ("**Cache results** — the same ORCID gets checked repeatedly across matcher re-runs"). |

## How this resolves the `work_mesh` open question

The docs repo records an unresolved inconsistency: two runbooks write MeSH tags to a `work_mesh`
link table, while the table reference and the DDL store them as a JSON array in `work.mesh_terms`
([ddl/README.md](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/database/ddl/README.md#known-inconsistency-work_mesh)).
The two shapes are not equivalent in cost: containment on `mesh_terms` answers "does this paper
cover descriptor D?" well, while a link table answers "how many distinct papers carry descriptor D?"
far better — and that second query is exactly the pruning step that decides which conditions exist.

`0006` takes the third option, which the DDL README leaves open ("add it here as a derived index
table alongside `mesh_terms`"):

- **`work.mesh_terms` stays canonical.** It is what the serving application reads, and the schema
  contract is unchanged.
- **`staging.work_mesh_expanded` is a derived expansion**, rebuilt from `mesh_terms` by the prune
  step. The counting queries — pruning, the specialty mapping, and the feature build — join against
  it instead of expanding JSON per pair.

Nothing is maintained in two places: the expansion is truncated and rebuilt, never updated. If
measurement later shows the expansion is expensive enough to want incrementally maintained, that is
the point at which `work_mesh` should be promoted into the canonical schema — and this table is
already its shape.
