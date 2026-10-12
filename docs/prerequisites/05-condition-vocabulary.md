# Prerequisites: Bucket 5 — Condition Vocabulary (MeSH)

> **Retired 2026-10-11.** The MeSH-derived vocabulary this runbook builds was replaced by generated concepts (`pex vocab …`, `src/pex_ingest/vocabulary/`), and `pex run conditions` now refuses to run. The current runbook is the docs repo's `data/ingestion/condition-vocabulary.md`; ADR 0005 there records why. This file is kept for history.

Everything that must be true before the condition-vocabulary pipeline runs. The only bucket that spends money, the only one with a hand-written input file, and the only one that can't finish until bucket 3 is loaded.

Runbook: [data/ingestion/condition-vocabulary.md](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/ingestion/condition-vocabulary.md)

## What this bucket produces, and why it's last

This builds the searchable condition list — the vocabulary a patient's query is matched against. It's generated, never hand-curated: MeSH provides the medical concepts, MedlinePlus provides patient-friendly names where it has them, and an LLM pass fills the rest plus the colloquial phrasings people actually type ("prostate problems", "elbow doctor").

It runs last because of one step: **pruning by publication frequency.** MeSH has ~30,000 descriptors, most of which no patient will ever search for. The filter that decides which ones become real conditions is "does our ingested literature actually write about this?" — which requires works in the database. The parse can run any time; the vocabulary *finalizes* after bucket 3.

## Scope: national, all specialties

The launch decision is **national coverage with all specialties**, which supersedes the narrowed-vertical assumption in [ADR 0001](https://github.com/davidcohn22/physician-expertise-docs/blob/main/architecture-decision-records/0001-v1-data-bucket-strategy.md). That resolves the blocker this bucket had — there's no longer a specialty list to wait on — but it replaces it with a design problem, and it changes what the candidate universe is.

### The candidate universe, measured

From `desc2026.gz` directly (31,110 descriptors total):

| Tree | What it is | Descriptors |
|---|---|---:|
| **C** | Diseases — the core of the vocabulary | **5,069** |
| **F03** | Mental Disorders | **254** |
| **E04** | Surgical Procedures | **887** |
| E01 | Diagnostic techniques | 1,215 |
| E02 | Therapeutics | 967 |
| _F01, F02, F04_ | _Behavior/psychological phenomena — **not** conditions ("Attention", "Memory")_ | _1,013_ |
| _D_ | _Chemicals & drugs — out of scope_ | _10,688_ |

Two defensible scopes:

- **Tight: C + F03 + E04 = ~6,200 descriptors.** Diseases, mental disorders, and surgical procedures — the things patients search when looking for a doctor.
- **Broad: add E01 + E02 ≈ ~8,000 descriptors.** Picks up "dialysis", "chemotherapy", "MRI"-type searches, at the cost of pulling in a lot of technique descriptors nobody searches by.

Start tight. E01/E02 can be added later by re-running the pipeline for those branches only — the cost of being wrong in either direction is a few dollars, not a rebuild.

Note **F01/F02/F04 must be excluded explicitly.** Taking all of `F` adds ~1,000 psychological-phenomena descriptors that look like conditions to a branch-prefix filter but aren't, and they will produce confident, useless synonyms.

### Cost: small, but measured

Measured 2026-10-10 with one live call on the real prompt: **357 input / 188 output tokens per descriptor** (the earlier 113/71 estimate was ~3× low). For ~6,200 descriptors, one call each, via the Batches API:

| Model | Live | **Batches (50% off)** | Thinking |
|---|---:|---:|---|
| **Claude Sonnet 5.5 (chosen)** | $16.10 | **$8.05** | off (`between_tools`) |
| Claude Opus 5.5 | $32.20 | $16.10 + thinking | cannot be turned off |
| Claude Haiku 5.5 | $0.81 | $0.40 | adaptive, can be disabled |

These are ceilings: pruning drops descriptors before enrichment. With a $20 API budget, Opus-tier leaves no room for a re-run; Sonnet 5.5 leaves room for two. The task (patient-friendly names and synonyms, fixed JSON schema) is recall, not reasoning, which is why thinking is off.

Batching ~20 descriptors per request cuts it further (~$5.55 at Opus 5) by amortizing the instruction block, but adds a join-back-by-index failure mode. **Not worth it at these amounts** — one descriptor per request with the descriptor UI as `custom_id` is safer and the saving is $1.50. Prompt caching doesn't apply either: at ~113 input tokens the prompt is far below the 512-token minimum cacheable prefix.

### The real cost of going national: the design, not the bill

**`branch_config.yaml` no longer works as specified.** Its purpose was to map *each launch specialty* to its MeSH branches by hand. "All specialties" makes that unwritable — there are ~100+ meaningful physician specialties and hand-authoring branch sets for each is exactly the manual work ADR 0001 ruled out.

The mapping has to invert. Instead of specialty → branches → conditions, derive `condition_taxonomy` from **which specialties' matched physicians actually publish on each descriptor**. The runbook already lists this as a cross-check; nationally it becomes the primary mechanism, and the config shrinks to a short list of included tree branches plus an exclusion list.

That is a genuine change to the runbook's step 5, and it means **this bucket now depends on bucket 3 more heavily than before** — not just for pruning, but for the specialty mapping itself.

Two other consequences:

- **The pruning threshold needs re-tuning.** The suggested ≥25 papers was sized for one narrow vertical against a partial corpus. Nationally, with all works ingested, far more descriptors clear it and the filter stops filtering. Consider a relative threshold (top-N per branch) rather than an absolute count.
- **The QA gate is now the bottleneck.** ~6,000 draft conditions to spot-check instead of a few hundred. Human review time, not dollars, is what scales badly here — plan a stratified sample (per tree branch, weighted by paper count) rather than a flat random one.

## Credentials

| Variable | How to get it |
|---|---|
| `ANTHROPIC_API_KEY` | [platform.claude.com](https://platform.claude.com) → API keys → Create key |

MeSH and MedlinePlus need nothing — both are free, public, unauthenticated NLM resources.

### Setting up the Anthropic key

1. Create an account at [platform.claude.com](https://platform.claude.com) and add a small credit balance (this bucket costs single-digit dollars; there's no subscription).
2. Create an API key and put it in `.env` as `ANTHROPIC_API_KEY`. The SDK's zero-argument constructor picks it up from the environment — never pass it as a literal:

```python
import anthropic
client = anthropic.Anthropic()      # reads ANTHROPIC_API_KEY from the environment
```

3. Confirm it works before building anything:

```bash
uv run python -c "
import anthropic
r = anthropic.Anthropic().messages.create(
    model='claude-sonnet-5-5', max_tokens=16,
    messages=[{'role':'user','content':'Reply with OK'}])
print(r.content[0].text, '| model:', r.model)
"
```

**On model choice:** `claude-sonnet-5-5` with thinking off (decided 2026-10-10, see [decisions/0002 §5](../decisions/0002-implementation-choices.md)). It's a short, narrow, high-volume prompt. Run `pex run conditions --enrich --live --limit 50` and read the sample before submitting the full batch, since this output becomes user-visible search vocabulary.

### Use the Batches API

At real volume — a few thousand descriptors, each a short prompt — run this through the Message Batches API rather than one live request per descriptor:

- **50% of standard token cost.**
- **No rate-limit management.** You submit the whole job and poll; there's no pacing loop to write or tune.
- Up to 100,000 requests or 256 MB per batch; most complete within an hour, 24 hours maximum.
- Results are retrievable for 29 days.

The shape: build one request per descriptor with a `custom_id` you can join back on, submit, poll `processing_status` until `"ended"`, then stream results.

**Results come back in arbitrary order — key them by `custom_id`, never by position.** This is the one mistake that silently corrupts the output: pair descriptor #400's synonyms onto descriptor #12 and you get a plausible-looking condition list that is quietly wrong in ways spot-checking may not catch. Use the MeSH descriptor UI (`D011470`) as the `custom_id` and there's nothing to misalign.

Also handle the non-success result types — a batch result is `succeeded`, `errored`, `canceled`, or `expired`, and treating all four as success means a descriptor silently gets no synonyms.

## What gets downloaded

| What | Size | Lands in | Mode |
|---|---:|---|---|
| MeSH descriptor XML | **16 MB** gzipped / 298 MB raw | `$PEX_DATA_DIR/mesh/2026/` | Download → stream-parse from disk |
| MedlinePlus health topics XML | **~32 MB** | `$PEX_DATA_DIR/mesh/2026/` | Download → parse from disk |
| Claude API | n/a | — | **Pure API** — Batches |
| `branch_config.yaml` | ~1 KB | **In this repo** | Hand-written; version-controlled |

### MeSH descriptor XML

```
https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/desc2026.gz     # 16 MB  ← use this
https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/desc2026.xml    # 298 MB
```

Both verified live 2026-08-15. **Take the `.gz`** — it's 18× smaller and `lxml` streams straight out of it. `desc2027.*` returns a 302 (not yet published); the new release lands each December, so an automated downloader should try the next year and fall back rather than hardcoding `2026`.

Everything this bucket needs is in this one file: descriptor IDs, names, tree numbers, and entry terms (official synonyms).

**Parse it streaming.** `lxml.iterparse` with `rec.clear()` after each record keeps memory flat; loading a 298 MB XML document as a tree will consume gigabytes for no reason.

The concept to understand before writing the parser: a **tree number** is a dotted path expressing where a concept sits in MeSH's hierarchy. `C12.294.565.500` reads as "urogenital diseases → male urogenital diseases → prostatic diseases → prostatic hyperplasia". A descriptor can have several tree numbers because a concept can sit in more than one place. Branch selection is prefix matching on those paths, which is why the config names branches like `C12` rather than listing thousands of descriptors.

### MedlinePlus health topics XML

Landing page: `https://medlineplus.gov/xml.html`

Files are dated — `mplus_topics_2026-08-14.xml` resolved live — so resolve the current one from the landing page rather than hardcoding. ~1,000 consumer-health topics, each carrying its MeSH heading: a ready-made "patient-friendly name for this descriptor" crosswalk, which is why the LLM only has to fill gaps rather than name everything.

## The hand-written input

`branch_config.yaml`, in this repo. The one file in the entire pipeline that a human authors — roughly a dozen lines per specialty.

```yaml
# branch_config.yaml — shape only; real branches TODO once launch specialties are chosen
urology:
  taxonomy_codes:     ["208800000X"]   # NUCC code(s) for the specialty
  mesh_branches:      ["C12"]          # urogenital diseases
  procedure_branches: ["E04.950"]      # urogenital surgical procedures
orthopedics:
  taxonomy_codes:     ["207X00000X"]
  mesh_branches:      ["C05"]          # musculoskeletal diseases
  procedure_branches: ["E04.555"]      # orthopedic procedures
```

**Validate every configured branch against the year's actual tree at parse time, and fail loudly if one is missing.** MeSH reorganizes its tree between annual releases, and a branch number that quietly stopped existing produces zero candidate conditions for that specialty — which looks identical to "this specialty has no conditions" and can survive a long way into a release before anyone notices.

The branch numbers above are illustrative, taken from the runbook. Verify each against `desc2026.gz` before trusting it.

## Prerequisites checklist

1. **`ANTHROPIC_API_KEY`** in `.env`, with a credit balance (~$10 covers one full run; budget is $20), verified with a live call.
2. **Included/excluded tree branches decided** — start with C + F03 + E04; exclude F01/F02/F04 and all of D explicitly.
3. **`branch_config.yaml` written** in its national form (branch include/exclude lists, not per-specialty stanzas — see [above](#the-real-cost-of-going-national-the-design-not-the-bill)), with every branch validated against `desc2026.gz`.
4. **Bucket 3 loaded** — needed for both the pruning step *and* the specialty mapping under national scope. The parse can run before this; promotion to `active` cannot.
5. **MeSH + MedlinePlus files downloaded** to `$PEX_DATA_DIR/mesh/2026/`.
6. **A stratified QA sampling plan**, since ~6,000 drafts is too many to review flat.

## Everything lands as `draft`

All four target tables get `status = 'draft'`. A spot-check pass promotes batches to `active`.

This is the only human-review gate in the pipeline, and it's deliberately a *sampling* gate rather than a full review: the QA step generates a review sheet — a random sample of drafts with their names, synonyms, and top matched papers — and a promotion script flips reviewed batches to `active`. Humans review samples; nothing is hand-authored.

It matters because this bucket's output is the only generated content a user reads as product copy. A bad synonym doesn't cause a crash — it causes a search that returns nothing, or worse, the wrong specialty.

## Sanity checks

| Check | Expected | If it's wrong |
|---|---|---|
| Descriptors parsed from `desc2026.gz` | ~30,000 | Streaming parse is dropping records |
| Every configured branch matched ≥1 descriptor | Yes | **Fail the run** — a branch number is stale |
| Candidates in `staging.mesh_candidates` | Hundreds to low thousands per specialty | Branch too broad (whole `C` tree) or too narrow |
| Surviving the ≥N-papers prune | Fewer; tune N empirically | If ~everything survives, N is too low to be doing any work |
| Conditions with a MedlinePlus display name | Minority | Expected — that's why the LLM pass exists |
| Conditions with ≥3 colloquial synonyms | Nearly all | Batch results not joined back correctly |
| Batch results joined by `custom_id` | **Verify explicitly** | Positional joining silently mismatches synonyms to conditions |
| Rows with `status = 'active'` before review | **0** | The QA gate is being bypassed |

The pruning threshold (the runbook suggests ≥25 papers) is the main tuning knob. Too low and the vocabulary fills with concepts nobody searches; too high and real conditions go missing. Descriptors that fail the threshold don't become conditions, but their papers still count toward their surviving *parent* descriptor via the child mappings — so a strict threshold loses less than it appears to.

```sql
ANALYZE condition;
ANALYZE condition_synonym;
ANALYZE condition_mesh;
ANALYZE condition_taxonomy;
```

## Refresh

| When | What |
|---|---|
| Annually (December) | New MeSH release → re-run the parse → diff against existing conditions. New descriptors become drafts; renamed or retired ones get flagged for review |
| After each publication load | Re-run the pruning query — newly ingested literature can promote a previously-thin descriptor past the threshold |
| On a new launch specialty | Add a config stanza, run the pipeline for just that specialty |

The annual refresh is a diff, not a rebuild — retired descriptors need a human decision, since silently dropping a condition breaks any saved link or bookmark pointing at it.

## Related

- [Runbook](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/ingestion/condition-vocabulary.md) — the parse, prune, and enrichment code
- [03-publications.md](03-publications.md) — must be loaded before the prune can run
- [README.md](README.md) — run order and open questions
