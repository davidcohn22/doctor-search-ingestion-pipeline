# Prerequisites: Bucket 1 — NPPES Physician Spine

Everything that must be true before the NPPES loader runs. This is the bucket that must succeed first: it creates the ~1M physician rows every other bucket joins against.

Runbook: [data/ingestion/identity-npi-nppes.md](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/ingestion/identity-npi-nppes.md)

## Credentials

**None.** Every endpoint here is free, public, and unauthenticated — including the Census geocoder. There is no account to create and nothing to approve.

## What gets downloaded

| # | What | Size | Lands in | Ingestion mode |
|---|---|---:|---|---|
| 1 | NPPES monthly full ZIP | **1.07 GB** (~10 GB extracted) | `$PEX_DATA_DIR/nppes/2026-08-full/` | Download → extract → read from disk |
| 2 | NPPES weekly incremental ZIP | **11 MB** | `$PEX_DATA_DIR/nppes/2026-08-03_2026-08-09/` | Download → extract → read from disk |
| 3 | NUCC taxonomy code list | ~1 MB | `$PEX_DATA_DIR/nucc/` | Download → read from disk |
| 4 | Census batch geocoder | n/a | `$PEX_DATA_DIR/geocode/` | **Pure API** — POST a CSV, get a CSV back |
| 5 | NPI Registry API | n/a | — | **Pure API** — single-record refresh only, never seeding |

Files 1–3 must be on disk. The 10 GB extracted CSV is read *in place* by DuckDB — it is never loaded into memory and never copied.

### 1–2. The NPPES files

Landing page: `https://download.cms.gov/nppes/NPI_Files.html`

Verified live 2026-08-15, the current files are:

```
https://download.cms.gov/nppes/NPPES_Data_Dissemination_August_2026_V2.zip           # 1.07 GB — monthly full
https://download.cms.gov/nppes/NPPES_Data_Dissemination_080326_080926_Weekly_V2.zip  #   11 MB — weekly incremental
https://download.cms.gov/nppes/NPPES_Deactivated_NPI_Report_081026_V2.zip            #        — deactivations
```

**The file names embed dates, so they change every month and every week.** There is no stable "latest" URL. An automated downloader must fetch the landing page and extract the current link:

```python
import os, re, requests

BASE = "https://download.cms.gov/nppes/"
HEADERS = {"User-Agent": f"physician-expertise-explorer ({os.environ['CONTACT_EMAIL']})"}

def current_nppes_urls():
    html = requests.get(BASE + "NPI_Files.html", headers=HEADERS, timeout=60).text
    zips = re.findall(r'href="\./([^"]+\.zip)"', html)
    monthly = next(z for z in zips if "Weekly" not in z and "Deactivated" not in z)
    weekly  = next(z for z in zips if "Weekly" in z)
    return BASE + monthly, BASE + weekly
```

Two automation gotchas, both discovered by trying it:

- **A browser `User-Agent` is required.** The landing page returns **HTTP 403** to a request without one, and 200 with one. This is the single most likely reason a first automated run fails, and the error looks like the file is missing rather than like a rejected request.
- **The links are relative** (`href="./NPPES_..."`), so they must be joined to the base URL. A naive scraper produces a relative path and a confusing failure.

Inside the monthly ZIP:

| File | Use |
|---|---|
| `npidata_pfile_<from>-<to>.csv` | **The main file.** ~8.5M rows, all provider types, ~330 columns. This is what gets staged. |
| `pl_pfile_*.csv` | Secondary practice addresses. **Not ingested at v1** — v1 stores the primary address only. Noted so nobody goes hunting for a separate download later. |
| `othername_pfile_*.csv` | Alternate names. Already represented in the main file's `Other Provider Name` columns. |
| `endpoint_pfile_*.csv` | Electronic endpoints. Not used. |

Use the **V2 layout** — mandatory since 2026-03-03. Column names differ from V1 (V1 used long strings like `Provider Last Name (Legal Name)`), so **verify header spellings against the header row of the file you actually downloaded** before trusting any column reference in the runbook.

### 3. NUCC taxonomy code list

The reference table that turns a taxonomy code like `207X00000X` into "Orthopaedic Surgery". Needed because bucket 1's "is this person a physician?" filter is a taxonomy-code test.

Landing page: `https://www.nucc.org/index.php/code-sets-mainmenu-41/provider-taxonomy-mainmenu-40/csv-mainmenu-57`

Verified current version as of 2026-08-15 is **26.1**:

```
https://www.nucc.org/images/stories/CSV/nucc_taxonomy_261.csv
```

The version number is `<year><release>` — `261` is 2026 release 1. NUCC publishes twice a year (January and July), and **old versions stay online**, so the page lists every historical file. A downloader should pick the numerically highest, not the first or last in document order:

```python
versions = re.findall(r'nucc_taxonomy_(\d+)\.csv', html)
latest = max(versions, key=int)      # '261'
```

### 4. Census batch geocoder

Turns practice addresses into latitude/longitude so "find a doctor within 25 miles" works. Free, no key.

```
POST https://geocoding.geo.census.gov/geocoder/locations/addressbatch
     benchmark=Public_AR_Current
     addressFile=@batch.csv          (multipart upload)
```

- Upload a **headerless** CSV of `unique_id,street,city,state,zip`, max **10,000 rows** per request.
- ~1M physicians ≈ ~100 requests total.
- Responses come back as CSV; keep both request and response in `$PEX_DATA_DIR/geocode/` so a failed batch can be retried without rebuilding it.
- Only geocode **new or address-changed** physicians. Coordinates for an unchanged address never go stale, and re-geocoding 1M addresses on every load is hours of wasted API traffic.
- Addresses the Census can't match (PO boxes, suite-only addresses) fall back to the ZIP-code centroid — coarse, but fine for radius search, and flagged so the UI can say "approximate".

A GET against this endpoint returns HTTP 400 — that's expected, it's POST-only. Don't read it as the endpoint being down.

## Preflight

```bash
# 1. Landing page reachable WITH a User-Agent (403 without one)
curl -sI -A "Mozilla/5.0" https://download.cms.gov/nppes/NPI_Files.html | head -1

# 2. Resolve and size the current monthly file without downloading it
curl -sI -A "Mozilla/5.0" \
  https://download.cms.gov/nppes/NPPES_Data_Dissemination_August_2026_V2.zip \
  | awk '/[Cc]ontent-[Ll]ength/{printf "%.2f GB\n", $2/1073741824}'

# 3. Room for 1 GB zipped + 10 GB extracted
df -h "$PEX_DATA_DIR" | tail -1

# 4. Target tables exist and are empty
psql -d pex -c "SELECT count(*) FROM physician;"          # expect 0 on a first run
psql -d pex -c "SELECT count(*) FROM taxonomy;"
```

## Sanity checks after loading

These are the numbers that tell you the load worked. A silent mis-parse of a 330-column file is entirely possible, and these catch it.

| Check | Expected | If it's wrong |
|---|---|---|
| Rows in the staged CSV | ~8.5M | Extraction truncated, or you read the wrong file in the ZIP |
| Rows after Entity Type Code = 1 filter | ~6M | Column name drift — verify against the actual header row |
| Rows after the taxonomy `20%` filter | **~1M** | The 15-slot taxonomy clause isn't being generated for all slots |
| Rows with a non-null `practice_state` | >95% | Address column mapping is off by one |
| Geocode match rate | >90% | Expected; the remainder falls back to ZIP centroid |
| `physician_taxonomy` rows | >1M (many physicians have several) | Only slot 1 is being read |

The ~1M figure is the load-bearing one: NPPES contains ~8.5M records of *all* provider types, and the two v1 filters — Entity Type Code = 1 (an individual human, not an organization) and at least one taxonomy code starting with `20` (the NUCC range for allopathic and osteopathic physicians, i.e. MDs and DOs) — are what narrow that to roughly a million actual doctors. If you get 8.5M, no filter applied. If you get 200K, the taxonomy clause is only checking one slot instead of all fifteen.

```sql
-- Post-load, always:
ANALYZE physician;
ANALYZE physician_taxonomy;
```

## Refresh

| When | What runs | Runtime |
|---|---|---|
| Weekly | Weekly incremental file → same filter/transform/upsert | minutes |
| Monthly | Full file as a true-up — catches anything a weekly missed, confirms deactivations | ~1 hour |
| Per load | Geocode only new/changed addresses | minutes |
| Twice a year | Refresh the NUCC taxonomy table (January, July) | seconds |

The weekly file is how retirees and deceased physicians leave search results: a previously-active NPI that appears with a deactivation date gets its `nppes_status` flipped rather than deleted, so the row and its provenance survive.

## Related

- [Runbook](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/ingestion/identity-npi-nppes.md) — the transform and upsert logic
- [00-workstation.md](00-workstation.md) — Python, Postgres, data directory
- [README.md](README.md) — run order and shared conventions
