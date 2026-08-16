# Scheduling — phase 1 (local `launchd`)

Jobs run on this Mac against local Postgres. This is the right place to start: bulk loads are fast
against a local database, and there is no monthly bill while the pipeline is still changing shape
every week. The honest limitation is that the laptop has to be awake and unlocked for a job to fire,
so a missed window just runs late — which is safe, because every job is an upsert and every long
walk is resumable.

**Phase 2** moves these schedules into GitHub Actions cron against hosted Postgres. That is a
configuration change rather than a rewrite because every job already reads `DATABASE_URL` and
`PEX_DATA_DIR` from the environment: `.env` becomes GitHub Actions secrets and the connection string
points somewhere else.

## Cadences

Straight from [docs/prerequisites/README.md](../../docs/prerequisites/README.md#scheduling-how-this-becomes-autonomous):

| Job | When | Command | Runtime |
|---|---|---|---|
| NPPES weekly | Mondays 02:00 | `run nppes --weekly` | minutes |
| NPPES monthly true-up | 1st of month 03:00 | `run nppes --seed` | ~1 hour |
| CMS medical school | Monthly, 4th 03:00 | `run cms` | minutes |
| Publications delta | Tuesdays 02:00 | `run publications --delta` | minutes |
| MeSH re-check sweep | Monthly, 5th 04:00 | `run publications --recheck` | minutes |
| Trials delta | Wednesdays 02:00 | `run trials --delta` | minutes |
| Trials full re-pull | Quarterly | `run trials --seed` | ~13 min |
| Snapshot re-sync | Quarterly | `run publications --pool --works` | hours |
| Condition re-prune | After each publication load | `run conditions --prune` | minutes |
| Feature rebuild | After **every** load | `features` | minutes |
| NUCC taxonomy | Twice a year (Jan, Jul) | `run nppes --taxonomy` | seconds |

**The feature rebuild is not optional and not independent.** `physician_condition_features` is what
the search actually reads; a load that isn't followed by a rebuild is invisible to the application.
Chain it after each ingest job rather than scheduling it separately, so it cannot drift.

## Installing one

```bash
sed -e 's|{{JOB}}|nppes-weekly|g' \
    -e 's|{{ARGS}}|<string>run</string><string>nppes</string><string>--weekly</string>|' \
    -e 's|{{WEEKDAY}}|1|; s|{{HOUR}}|2|' \
    -e "s|{{REPO}}|$PWD/../..|g" \
    com.pex.ingest.template.plist > ~/Library/LaunchAgents/com.pex.ingest.nppes-weekly.plist

launchctl load ~/Library/LaunchAgents/com.pex.ingest.nppes-weekly.plist
launchctl list | grep com.pex.ingest
```

`launchd` does not inherit your shell environment, so the job reads `.env` at the repo root (which
is why `WorkingDirectory` matters) rather than relying on exported variables.

## Before trusting any schedule

1. Run the job by hand once and check `pex status`.
2. Kill a long job halfway and re-run it — resume is the property everything else depends on, and it
   should be tested deliberately rather than discovered during an overnight run.
3. Confirm **Start at login** is on in Postgres.app settings. Otherwise a scheduled job fires against
   a stopped server, and the only symptom is a connection error in a log nobody is reading.
