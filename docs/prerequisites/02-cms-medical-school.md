# Prerequisites: Bucket 2 — CMS Medical School

Everything that must be true before the CMS Doctors & Clinicians loader runs. The smallest and easiest of the five buckets: one CSV, no credentials, minutes of runtime.

Runbook: [data/ingestion/medical-school.md](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/ingestion/medical-school.md)

## Credentials

**None.** Free, public, unauthenticated.

## Prerequisite

**Bucket 1 must be loaded.** This file covers every Medicare-enrolled clinician type — social workers, chiropractors, physical therapists — and only the rows whose NPI already exists in `physician` are kept. Run it against an empty spine and it inserts nothing, silently.

## What gets downloaded

| What | Size | Lands in | Ingestion mode |
|---|---:|---|---|
| CMS Doctors & Clinicians National Downloadable File | **0.78 GB** | `$PEX_DATA_DIR/cms-dac/2026-07/` | Download → read from disk |
| Query API | n/a | — | **Pure API** — spot lookups only |

### Resolving the download URL

The direct file URL contains a content hash and changes every time CMS republishes, so it cannot be hardcoded. The **stable entry point is the metastore endpoint** for dataset `mj5m-pzi6`:

```
GET https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/mj5m-pzi6
```

```python
import requests

meta = requests.get(
    "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/mj5m-pzi6",
    timeout=30,
).json()
csv_url  = meta["distribution"][0]["downloadURL"]
modified = meta["modified"]        # use this to name the snapshot directory
```

Verified live 2026-08-15 — the metastore returns:

```
title:    National Downloadable File
modified: 2026-07-31
url:      https://data.cms.gov/provider-data/sites/default/files/resources/
          52c3f098d7e56028a298fd297cb0b38d_1785521776/DAC_NationalDownloadableFile.csv
```

Use `meta["modified"]` for the snapshot directory name (`cms-dac/2026-07/`) — it's the authoritative release date, and it makes "have I already got this release?" a string comparison instead of a re-download.

**Note the size:** 0.78 GB, not the 2–3 GB the runbook estimates. CMS has trimmed the column set since that was written. Not a problem, but if a preflight expects 2 GB it will look like a truncated download.

### Query API (spot lookups only)

```
GET https://data.cms.gov/provider-data/api/1/datastore/query/mj5m-pzi6/0
    ?conditions[0][property]=NPI&conditions[0][value]=<npi>
```

Free, no auth, JSON, max 2,000 rows per request. For debugging a single physician's record. **Never the seeding path** — 1M single-record calls to reproduce one 780 MB file is exactly the backwards approach the bulk file exists to avoid.

### Data dictionary

[DOC_Data_Dictionary.pdf](https://data.cms.gov/provider-data/sites/default/files/data_dictionaries/DOC_Data_Dictionary.pdf) — column definitions. **The column set drifts between releases**, so re-check it on each seed rather than trusting a cached column list.

## The one structural gotcha

**The file has one row per clinician × practice location.** A physician with four offices appears four times. Left undeduplicated, you get four `physician_education` rows per doctor and a primary-key conflict or four times the expected row count.

The dedupe is clean because `Med_sch` and `Grd_yr` are identical across a clinician's rows — they come from that doctor's single PECOS enrollment record, not from the location. So `SELECT DISTINCT` on the three columns collapses it correctly:

```sql
SELECT DISTINCT NPI, Med_sch, Grd_yr
FROM read_csv('DAC_NationalDownloadableFile.csv', header=true)
WHERE Med_sch IS NOT NULL
```

Inspect it before writing the loader — this is exactly what the DuckDB CLI is for:

```bash
duckdb -c "
  SELECT count(*) AS rows,
         count(DISTINCT NPI) AS distinct_npis,
         round(count(*)::float / count(DISTINCT NPI), 2) AS rows_per_npi
  FROM read_csv('$PEX_DATA_DIR/cms-dac/2026-07/DAC_NationalDownloadableFile.csv', header=true);
"
```

Expect ~2.6M rows against a smaller distinct-NPI count. If `rows_per_npi` is 1.0, either the file changed shape or you're reading a pre-filtered extract.

## `Med_sch = "OTHER"` is data, not a null

A meaningful share of rows carry the literal string `OTHER` as the medical school. **Store it unchanged.** It is informative — it correlates strongly with international medical training — so treating it as missing throws away a real signal. Display logic decides how to present it; the loader does not editorialize.

The enrichment chain for `OTHER` (ORCID → state rosters → compliant directory scraping) is explicitly **v2**. Do not build it now.

## Two data-quality guards to build in from day one

**1. `Grd_yr` sanity check.** Cast failures and absurd years (before 1930, after the current year) go to a rejects report, not into the table. A graduation year of `19` or `2087` in a physician profile is the kind of visible-and-embarrassing error this bucket is otherwise too easy to ship.

**2. Coverage report.** After each load, log:

```sql
SELECT
  (SELECT count(*) FROM physician)                                    AS spine,
  count(*)                                                            AS with_education,
  round(100.0 * count(*) / (SELECT count(*) FROM physician), 1)        AS pct_covered,
  round(100.0 * count(*) FILTER (WHERE med_school = 'OTHER')
        / count(*), 1)                                                AS pct_other
FROM physician_education WHERE source = 'cms';
```

`pct_other` is the number that decides whether the v2 enrichment chain is worth prioritizing. Capture it from the first load so you have a baseline rather than a guess.

## Preflight

```bash
# 1. Metastore resolves and reports a release date
curl -s "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/mj5m-pzi6" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
      print('modified:', d['modified']); print('url:', d['distribution'][0]['downloadURL'])"

# 2. Spine is loaded — this bucket is a no-op without it
psql -d pex -c "SELECT count(*) FROM physician;"    # want ~1M, NOT 0

# 3. Room for ~1 GB
df -h "$PEX_DATA_DIR" | tail -1
```

## Sanity checks after loading

| Check | Expected | If it's wrong |
|---|---|---|
| Raw CSV rows | ~2.6M | Truncated download |
| Distinct NPIs in CSV | < raw rows | Fine — that's the multi-location duplication |
| `physician_education` rows written | ≤ distinct NPIs | More than distinct NPIs means the dedupe didn't apply |
| Spine coverage | Report it, don't assert it | This is a new baseline number, not a known one |
| `confidence` on every row | `1.0` | CMS keys by NPI directly, so there is no matching and no uncertainty |
| `Grd_yr` rejects | Small and inspectable | A large reject count means a column-position problem |

```sql
ANALYZE physician_education;
```

## Refresh

**Monthly** — re-resolve the URL from the metastore, compare `modified` against the last run, skip if unchanged, otherwise re-pull and re-run. Minutes of runtime, no pacing concerns, no API budget. The upsert makes re-runs free of side effects.

## Related

- [Runbook](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/ingestion/medical-school.md) — the transform and upsert logic
- [01-nppes-identity.md](01-nppes-identity.md) — the spine this bucket joins against
- [README.md](README.md) — run order and shared conventions
