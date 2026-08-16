# Prerequisites: Bucket 4 — Clinical Trials (ClinicalTrials.gov)

Everything that must be true before the trials loader runs. No credentials, no bulk file, ~12 minutes for the entire world corpus — the best cost-to-value ratio of the five buckets.

Runbook: [data/ingestion/clinical-trials.md](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/ingestion/clinical-trials.md)

## Credentials

**None.** No key, no registration, no rate-limit tier. The data is US federal government public domain.

## Prerequisite

**Bucket 1 must be loaded.** Like publications, this bucket matches names against the physician spine. Unlike publications, it has no expensive fetch phase — the corpus is smaller than our physician list.

## What gets downloaded

| What | Size | Lands in | Mode |
|---|---:|---|---|
| Full study corpus | ~2 GB with field filtering | `$PEX_DATA_DIR/ctgov/2026-08/` | **Pure API** — paged, cached to JSONL |
| Weekly delta | small | same | **Pure API** |

There is no bulk ZIP to download. Everything comes through the v2 REST API, and the local JSONL cache exists so a re-parse doesn't mean a re-pull.

Verified live 2026-08-15:

```bash
curl -s "https://clinicaltrials.gov/api/v2/stats/size"
# {"totalStudies":598690,"averageSizeBytes":17278, ...}
```

**598,690 studies worldwide**, ~17 KB average. At `pageSize=1000` that's ~599 calls; at the polite ~46 requests/minute pace, **~13 minutes** for the entire corpus.

### Take the whole world

Filtering to US-only studies saves almost nothing and risks missing a US site on a foreign-sponsored trial — which is exactly the kind of gap that makes a profile look incomplete for no good reason. Pull everything.

### Field filtering is what keeps it small

The `fields=` parameter is the difference between ~10 GB and ~2 GB. Request only what's surfaced or matched on:

```text
NCTId,BriefTitle,BriefSummary,Condition,ConditionMeshTerm,InterventionType,
InterventionName,InterventionMeshTerm,OverallStatus,StartDate,PrimaryCompletionDate,
OverallOfficialName,OverallOfficialAffiliation,OverallOfficialRole,
LocationFacility,LocationCity,LocationState,LocationContactName,LocationContactRole,
LastUpdatePostDate
```

The field paths come from the API's own self-documenting metadata endpoint — check it rather than guessing, since names drift:

```bash
curl -s "https://clinicaltrials.gov/api/v2/studies/metadata" | head -c 2000
```

### Endpoints

| Purpose | Call |
|---|---|
| Seed (full corpus) | `GET /api/v2/studies?pageSize=1000&fields=<list>` then follow `nextPageToken` |
| Weekly delta | `GET /api/v2/studies?query.term=AREA[LastUpdatePostDate]RANGE[<lastRun>,MAX]&pageSize=1000&fields=<list>` |
| Single study | `GET /api/v2/studies/<nctId>` |
| Corpus size | `GET /api/v2/stats/size` |
| Field reference | `GET /api/v2/studies/metadata` |

Only **~5,700 studies change in a typical week**, so one paged delta query catches new trials and status changes on tracked ones.

**Never** use per-name search (`query.term=AREA[OverallOfficialName]<name>`) as the seeding path. It's loose text matching — every hit is a candidate, not a match — and per-physician searching would be 2M+ calls to reproduce a corpus you can download in 13 minutes.

## Pacing

`time.sleep(1.3)` between calls ≈ 46 requests/minute, under the ~50/min limit. There is no published hard quota, which is exactly why staying comfortably under matters — an unthrottled scraper is how a free public API acquires a rate limit.

Persist `nextPageToken` after every page. 599 sequential calls is short enough to feel safe and long enough that a dropped connection at call 400 is annoying.

## The structural thing to understand before you build this

**Most physicians appear as a *site contact*, not as the national lead.** A trial names people in two distinct places:

| Field | Who it is | Coverage |
|---|---|---|
| `overallOfficials` | Principal investigator for the whole study; carries an affiliation string | Rare per physician |
| `locations[].contacts` | Contact at one participating site; carries that site's facility, city, state | **The common case** |

A matcher that only reads `overallOfficials` will find a handful of famous investigators and miss almost everyone. Both fields matter, and the site contact is the higher-volume one — with the bonus that its facility/city/state is *better* geographic matching evidence than an overall official's free-text affiliation string.

The flow is therefore: slim each study → extract every named person as a mention → flip into a name-keyed index (`name → (trial, role, affiliation/facility, geography)`) → match that index against the spine. Building the index first is what makes matching a local join rather than 600k scans.

### Name normalization

Trial names arrive as `"Sarfaraz A Hasni, M.D."` — credentials and punctuation attached. Strip them, then key on normalized last name + first initial. **Use the same normalization and the same candidate-lookup code as the publications matcher** (bucket 3), so the two buckets share one definition of "same name" rather than drifting apart.

Never accept on name alone. Common surnames over-match badly, and the same asymmetry from bucket 3 applies: a missing trial is a small gap, a wrong trial on a profile is a credibility failure.

## Preflight

```bash
# 1. API reachable and corpus size known
curl -s "https://clinicaltrials.gov/api/v2/stats/size" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('{:,} studies'.format(d['totalStudies']))"

# 2. One page comes back with the fields you asked for
curl -s "https://clinicaltrials.gov/api/v2/studies?pageSize=1&fields=NCTId,BriefTitle" | head -c 400

# 3. Spine is loaded
psql -d pex -c "SELECT count(*) FROM physician;"   # want ~1M

# 4. Target tables exist
psql -d pex -c "\d trial"
psql -d pex -c "\d trial_investigator"
```

## Sanity checks

| Check | Expected | If it's wrong |
|---|---|---|
| Studies pulled | ~598,690 | Paging stopped early — check `nextPageToken` handling |
| `trial` rows | = studies pulled | Upsert key isn't `nct_id` |
| Person mentions extracted | Several per trial on average | Only one of the two name fields is being read |
| Mentions from `location_contact` vs `overall_official` | **Site contacts should dominate** | If overall officials dominate, `locations[].contacts` isn't being traversed |
| Trials with `mesh_ids` | Substantial share | `derivedSection.conditionBrowseModule` not being read |
| `trial_investigator` rows with role + site + confidence | All | Provenance columns not populated |

The MeSH point is worth noting: trial condition MeSH descriptors come from the same descriptor space as publication MeSH terms, so **one condition-relevance signal spans both buckets**. That's what lets a search for a condition rank publications and trials on the same footing, and it's a reason to get the field extraction right rather than treating trials as a separate island.

```sql
ANALYZE trial;
ANALYZE trial_investigator;
```

## Refresh

| When | What | Runtime |
|---|---|---|
| Weekly | Delta query → upsert changed studies by `nct_id` → re-match **only those trials' name keys** | minutes |
| Quarterly | Full re-pull as a cheap backstop against missed deltas | ~13 min |

On a delta, a changed trial's person mentions **replace** the old mentions for that trial — delete-then-insert inside one transaction, which is simpler and less bug-prone than diffing two mention lists. The whole loop stays inside the free public API: no key, no cost, no budget.

## Related

- [Runbook](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/ingestion/clinical-trials.md) — the pager, slimming, and name-key code
- [03-publications.md](03-publications.md) — shares the matching machinery and the MeSH descriptor space
- [README.md](README.md) — run order and shared conventions
