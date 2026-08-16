# V1 open questions

A plain-language roundup of what's still undecided across both repos ([physician-expertise-docs](https://github.com/davidcohn22/physician-expertise-docs) and this one) as of 2026-08-15 — kept short on purpose. It skips cosmetic TODOs (stub sections, "expand this later" notes) and focuses on things that either block ingestion work directly, or need an answer before the next phase of the app (the serving application, the ranking formula) can move forward. Update or trim entries as they get resolved; don't let this become a second stale backlog.

## Fix first: the two repos disagree about launch scope

The docs repo still says the launch specialties are undecided. This repo's own prerequisites doc says the opposite — that the question is resolved, and resolved differently than the docs repo assumed:

- **Docs repo (`physician-expertise-docs`)**: [ADR 0001](https://github.com/davidcohn22/physician-expertise-docs/blob/main/architecture-decision-records/0001-v1-data-bucket-strategy.md) says v1 launches with 2–3 "complex-condition" specialties — candidates named are orthopedics, neurosurgery, urology — and marks the actual choice **TODO**. [`CURRENT_STATE.md`](https://github.com/davidcohn22/physician-expertise-docs/blob/main/CURRENT_STATE.md) and [`data/data-sources/README.md`](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/data-sources/README.md) both still frame the project this way — a narrow launch vertical of ~20–50K physicians.
- **This repo**: [`docs/prerequisites/README.md`](prerequisites/README.md) states, dated 2026-08-15: *"Launch scope is national, all specialties (supersedes the narrowed-vertical assumption in ADR 0001)."*

Nobody has gone back and updated the docs repo to reflect this reversal. That matters beyond bookkeeping — the whole rationale in ADR 0001 (buying individual state license rosters, checking hospital sites one by one, a ~200-pair matching eval set) was sized for tens of thousands of physicians, not the ~1M-physician national NPPES universe. Every downstream estimate in that ADR (cost, legwork, eval-set size) needs re-checking against national scope, not just the headline scope line.

**Needs a human decision:** confirm national/all-specialty is really the intended v1 scope, then update ADR 0001's status, `CURRENT_STATE.md`, and `data/data-sources/README.md` in the docs repo to match — or reverse course back to a narrowed vertical in this repo if national scope was a mistake. Either way, the two repos need to agree before more downstream work is built on top of whichever assumption is wrong.

## Blocking decisions for ingestion work already in flight

These are things the ingestion pipeline cannot be correctly finished without answering — not aspirational, not deferred-with-a-trigger.

### 1. OpenAlex works: bulk snapshot or per-author API calls?

[`docs/decisions/0001-openalex-works-via-snapshot.md`](decisions/0001-openalex-works-via-snapshot.md) — status: **proposed, needs sign-off.**

This is the single biggest cost/timeline lever in the whole pipeline. OpenAlex is the database of academic publications this product uses to find a physician's papers. The existing runbook plans to call OpenAlex's API once per matched physician to walk their paper list — priced at roughly $50–150 one time, or 2–5 months if paced inside the free daily budget. The proposal on the table instead downloads OpenAlex's public bulk data file once (anonymously, no account needed) and joins it locally against matched physicians — measured at ~$0 and a few hours, because only the ~12% of each record actually needed (identity, title, year, journal, authors, MeSH tags) has to be read, not the full file. The tradeoff: the free bulk file only refreshes quarterly, so it needs to be paired with lightweight weekly API calls to stay current between refreshes.

**Needs a human decision:** accept, reject, or amend the snapshot-plus-weekly-deltas approach. If accepted, the publications runbook in the docs repo needs a real edit (its "OpenAlex API only" cost/timeline table is currently wrong), which the decision doc explicitly flags as something that "should be raised there rather than made unilaterally."

### 2. Where do a paper's MeSH tags live: a separate table, or a JSON column?

[`data/database/ddl/README.md#known-inconsistency-work_mesh`](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/database/ddl/README.md) (docs repo) and [`docs/prerequisites/README.md`](prerequisites/README.md) (this repo) both flag it.

MeSH tags are the standardized medical-subject labels (e.g., "Prostatic Hyperplasia") that PubMed attaches to a paper, and they're what lets the app say "this doctor has published on this condition." Two of the ingestion runbooks (publications, condition-vocabulary pruning) were written assuming those tags live in their own table, `work_mesh` — one row per (paper, tag). The actual database schema instead stores them as a JSON array directly on the paper's row (`work.mesh_terms`), with an index built for that shape. The two shapes aren't just a style difference: the JSON-array shape answers "does this paper cover condition X?" cheaply, while a separate table answers "how many papers cover condition X?" cheaply — and that second question is exactly what the condition-vocabulary pruning step needs.

**Needs a human decision:** pick one shape and fix the runbooks (and the pruning query) to match. The docs repo currently says this is deliberately left open "until the publications load is real enough to measure" — but the pruning query can't be written correctly either way until it's picked.

### 3. Condition-vocabulary mapping doesn't scale to national/all-specialty scope

[`docs/prerequisites/05-condition-vocabulary.md`](prerequisites/05-condition-vocabulary.md) and [`docs/prerequisites/README.md`](prerequisites/README.md).

The condition vocabulary is the list of searchable medical conditions (e.g., "elbow arthritis") the app is built around, generated from the MeSH medical taxonomy. The original design assumed a small, hand-written map from each launch specialty to its relevant branch of that taxonomy (`branch_config.yaml`). That doesn't scale to "all specialties" — nobody is going to hand-write ~100+ specialty mappings. The fix direction sketched in the doc is to flip the logic: instead of a human mapping specialty → MeSH branch, derive it from which specialties the physicians who actually publish on a given MeSH term belong to. That's described as a real rewrite of a runbook step, not yet written.

**Needs a human decision:** confirm this derived-mapping approach and get it written up as the new runbook step (this is downstream of the scope question above — it only matters if national/all-specialty scope is confirmed).

### 4. The MeSH pruning threshold is a placeholder, not a tuned number

[`data/data-sources/condition-vocabulary.md`](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/data-sources/condition-vocabulary.md) (docs repo) and [`docs/prerequisites/05-condition-vocabulary.md`](prerequisites/05-condition-vocabulary.md).

To keep the condition list from including obscure MeSH terms nobody searches for, the pipeline only keeps a term if at least N papers in the database are tagged with it. N is currently a placeholder (≥25), picked for a narrow launch vertical. At national scale, far more terms will clear a flat count of 25, so the filter stops doing useful filtering. The fix direction noted in the doc is a relative threshold (e.g., top-N terms per specialty branch) instead of an absolute count — not yet decided or tuned.

**Needs a human decision + empirical tuning**, once real publication data is loaded and the mapping in item 3 is settled.

### 5. No defined QA process for the LLM-generated condition names/synonyms at scale

[`data/data-sources/condition-vocabulary.md`](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/data-sources/condition-vocabulary.md) and [`docs/prerequisites/05-condition-vocabulary.md`](prerequisites/05-condition-vocabulary.md).

Patient-friendly condition names and colloquial synonyms (e.g., "elbow doctor" → "lateral epicondylitis") are drafted by an LLM pass and are supposed to be spot-checked by a human before going live, never auto-published. At national scope that's roughly 6,000 drafted condition entries — explicitly called out in the doc as likely "the bottleneck" — and there's no defined sample size, acceptance bar, or stratification plan (e.g., sampling evenly across specialties rather than randomly, so a systematic error in one specialty doesn't slip through as a rounding error in a flat random sample).

**Needs a human decision:** a concrete QA sampling plan and acceptance bar before the vocabulary can be trusted to go live unattended.

### 6. The physician↔author matching eval set may not represent national scale, and hasn't been spot-checked yet

[ADR 0001](https://github.com/davidcohn22/physician-expertise-docs/blob/main/architecture-decision-records/0001-v1-data-bucket-strategy.md) (docs repo) and [`docs/prerequisites/README.md`](prerequisites/README.md).

Entity resolution — matching a name on a published paper to the correct physician's NPI (National Provider Identifier) — is called out repeatedly as the core technical risk of the whole pipeline: a wrong match puts someone else's paper on a physician's profile, which is about the worst credibility failure this product could ship. The current eval set ([`eval/physician_author_pairs.csv`](../eval/physician_author_pairs.csv), 200 pairs) was generated by [`scripts/bootstrap_eval_set.py`](../scripts/bootstrap_eval_set.py) for a narrow launch vertical, and two things about it are still open:

- It's explicitly **not yet validated ground truth** — it was bootstrapped algorithmically and is meant to be spot-checked by a human before being trusted, and the docs give no indication that spot-check has actually happened yet.
- At national, all-specialty scope, name-collision risk varies a lot by specialty (surname distributions, how common international training/naming patterns are), so 200 pairs concentrated in a couple of specialties may not represent the matcher's real error rate nationally. The prerequisites doc suggests stratifying the eval set by specialty instead, but hasn't done it.

**Needs a human action:** actually run the spot-check on the existing 200 pairs, and decide whether/how to expand or re-stratify the eval set for national scope before trusting matcher precision numbers.

## Ingestion-adjacent legwork that's described but not confirmed done

These aren't open design questions so much as tasks the docs say should happen early, without any signal they've been started:

- **Which state license-board rosters to actually buy, and at what price.** V1's license/discipline-data plan is to buy state physician-license rosters as public records (many states sell their full licensee list for a small fee). ADR 0001 says to "verify availability and price per state" — that verification doesn't appear to have happened yet.
- **State public-records audit emails.** For the roughly dozen states with physician-profile public-records laws, ADR 0001 calls for sending audit-request emails early because they're "cheap to send, slow to return." No evidence they've been sent.
- **A real legal sanity-check on the compliance-gated scraping plan.** Several v1 buckets (residency/fellowship, board certification, medical-school gap-filling) fall back on scraping hospital/academic "Find a Doctor" directories for bare facts, under a self-designed compliance protocol (ToS/robots.txt pre-checks, no logins, immediate stop-and-delete on objection). The docs repeatedly flag this as "our own read, not legal advice — sanity-check before launch," but nothing indicates that sanity-check has happened. Worth doing before the scraper is actually pointed at a live hospital site, since several buckets depend on this fallback working.

## Correctly deferred, not blocking v1 — for awareness only

These already have a decision and an explicit trigger for revisiting later; they don't need action now, just flagging so they don't get lost:

- **Program-quality scoring for residency/fellowship** (homegrown publications-by-institution proxy vs. licensing Doximity Residency Navigator vs. dropping it) — undecided, but not urgent; no free source exists either way.
- **The recency window for "recent publications"** on a physician's condition profile — currently an unspecified constant, "likely 5 years."
- **Which managed Postgres provider to use once the app is actually serving traffic** (Neon, Supabase, AWS RDS all named as candidates) — explicitly deferred until going live is near.
- **`pgvector` semantic search and a dedicated search engine (Typesense/Meilisearch/OpenSearch)** — both deferred until real search logs show the current trigram/synonym matching is actually missing queries; no query logging exists yet to even measure that.
- Patient reviews, insurance-carrier coverage, and true all-payer procedure volume are all **v1: link-out or scoped-signal only, by design** — their fuller versions are explicitly v2-or-later and don't need a decision now.

## Not ingestion, but blocks the next phase of the app

- **The ranking formula is entirely unwritten.** The product's philosophy (show evidence, not an opaque score) is settled, but no actual formula exists yet for turning the "ingredient" numbers this pipeline produces (relevant-publication count, first/last-author count, trial count, etc.) into a ranked list. This doesn't block ingestion — the pipeline just needs to keep producing those ingredient numbers correctly — but it blocks the serving application, and the schema docs note ranking logic intentionally lives in app code, not the database, specifically so it can be iterated without a migration.
- **The docs repo's own "Undecided / open questions," "In progress," and "Not started" sections in [`CURRENT_STATE.md`](https://github.com/davidcohn22/physician-expertise-docs/blob/main/CURRENT_STATE.md) are still blank placeholders.** Nobody has done a top-level inventory of what's blocking progress in that repo — this document is effectively a first pass at that inventory from the ingestion side; the docs repo would benefit from its `CURRENT_STATE.md` being filled in and kept current the same way.

## If you only resolve five things next

1. Reconcile the launch-scope disagreement between the two repos (national vs. narrowed vertical) — everything else takes a different shape depending on the answer.
2. Sign off on the OpenAlex snapshot-vs-API decision — it's a multi-month timeline swing sitting idle.
3. Pick the `work_mesh` vs. `work.mesh_terms` schema shape — it silently blocks writing the publications load and the vocabulary pruning query correctly.
4. Run the eval-set spot-check that's already described but not done, before trusting any matcher precision numbers.
5. Send the state public-records audit emails and price out the license rosters — both are described as "start the clock early" tasks that don't appear to have started.
