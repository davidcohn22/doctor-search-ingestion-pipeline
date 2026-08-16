# Workstation Setup

The one-time setup shared by every ingestion bucket: a current Python, the data directory, the database connection, and secrets. Do this once; the per-bucket docs assume it's done.

Budget about 30 minutes, most of it waiting on downloads.

## 1. Python 3.12+ — and the trap

macOS ships a `python3` that comes from Apple's Command Line Tools, and it is **3.9**. Several libraries this pipeline needs have dropped support for it, and it isn't ours to upgrade. Check what you have:

```bash
python3 --version
```

If that prints 3.9.x, install a current Python separately. The recommended route is [`uv`](https://docs.astral.sh/uv/) — a single fast tool that manages Python versions *and* replaces `pip` and `virtualenv`:

```bash
brew install uv
```

Then, from the repo root:

```bash
uv init                 # only if pyproject.toml doesn't exist yet
uv add psycopg[binary] duckdb requests lxml rapidfuzz python-dotenv anthropic pyarrow
```

`uv run script.py` from then on uses the right interpreter with the right dependencies, with no virtualenv to activate or forget.

What each dependency is for, since several are unfamiliar unless you've done data work before:

| Package | What it is | Why we need it |
|---|---|---|
| `psycopg[binary]` | The PostgreSQL driver for Python | How every row gets written. The `[binary]` extra bundles `libpq`, so there's no separate C library to install. |
| `duckdb` | An embedded analytics database | Runs SQL directly over multi-gigabyte CSV and Parquet files **without loading them into memory**. This is how we chew through the 10 GB NPPES file on a laptop. |
| `pyarrow` | Columnar data library | Reads Parquet, including only the columns we ask for. Central to the OpenAlex snapshot path (bucket 3). |
| `requests` | The standard HTTP library | Every API call. |
| `lxml` | A fast XML parser | Used in *streaming* mode so the 298 MB MeSH file never sits in memory whole. |
| `rapidfuzz` | Fast string similarity | The physician↔author name matching in buckets 3 and 4. |
| `python-dotenv` | Reads `.env` into the environment | One line, keeps secrets out of the repo. |
| `anthropic` | The Claude API SDK | Bucket 5's synonym generation. |

`splink` (probabilistic record linkage) is a deliberate later addition — it's the upgrade path if hand-tuned matching weights plateau against the eval set, not a day-one dependency.

## 2. PostgreSQL with the v1 schema

Full walkthrough, written for someone coming from SQL Server and SSMS, lives in the docs repo: [data/database/database-setup.md](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/database/database-setup.md). The short version:

```bash
# Install Postgres.app from https://postgresapp.com, click Initialize, then:
pg_isready                                        # want: accepting connections
createdb pex

# From the docs repo:
cd data/database/ddl
psql -d pex -f run-all.sql                        # 16 serving + 5 staging tables, safe to re-run
psql -d pex -f 08-roles-and-grants.sql            # EDIT THE PASSWORDS FIRST — not re-runnable
psql -d pex -f verify.sql                         # smoke test
```

Two details that matter for ingestion specifically:

- **Turn on "Start at login"** in Postgres.app settings. Otherwise a scheduled job fires against a stopped server, and the only symptom is a connection error in a log you aren't reading.
- **The pipeline connects as `pex_ingest`**, not as your superuser account. `pex_ingest` has read/write on both schemas; the .NET API's `pex_app` role is read-only on the serving schema. That split means "the API never writes" is enforced by the database rather than by discipline.

## 3. The data directory

```bash
mkdir -p ~/pex-data/{nppes,cms-dac,openalex/{authors,works},ctgov,mesh,nucc,geocode,_archive}
```

Then export it so every job and every shell can find it. Add to `~/.zshrc`:

```bash
export PEX_DATA_DIR="$HOME/pex-data"
export PGDATABASE=pex          # lets you type `psql` with no arguments
```

Open a new terminal and confirm:

```bash
echo "$PEX_DATA_DIR" && test -w "$PEX_DATA_DIR" && echo writable
df -h "$PEX_DATA_DIR" | tail -1   # want 150 GB+ free
```

The layout and the reasoning behind keeping it outside the repo are in [README.md](README.md#where-files-live).

## 4. Secrets

```bash
cp .env.example .env
$EDITOR .env
```

Fill in `DATABASE_URL`, `PEX_DATA_DIR`, and `CONTACT_EMAIL` now; add the three API keys as you reach the buckets that need them ([full table](README.md#credentials-and-where-they-go)).

Confirm the file is ignored before you put a real password in it:

```bash
git check-ignore -v .env    # want: a line naming .gitignore
```

If that prints nothing, **stop** — `.env` is not ignored and the next commit will publish your database password.

For anything you run through `psql` or the CLI tools, a `~/.pgpass` file is better than exporting `PGPASSWORD`:

```bash
echo "localhost:5432:pex:pex_ingest:YOUR_PASSWORD" >> ~/.pgpass
chmod 600 ~/.pgpass          # libpq refuses to read it otherwise
```

## 5. Optional but genuinely useful

| Tool | Install | Why bother |
|---|---|---|
| **DuckDB CLI** | `brew install duckdb` | Lets you point SQL at a multi-GB CSV *before* writing any loader code: `duckdb -c "SELECT * FROM read_csv('f.csv') LIMIT 5"`. The fastest way to answer "what does this government file actually contain?" |
| **DBeaver** | `brew install --cask dbeaver-community` | The closest thing to SSMS ergonomics for Postgres, and free. |
| **Jupyter** | `uv add --dev jupyterlab` | The natural place to tune a matcher and eyeball match rates before hardening the logic into a script. |

## Verification

Run all of this before starting bucket 1. Every line should succeed.

```bash
python3 --version                                    # 3.12+
uv run python -c "import psycopg, duckdb, lxml, rapidfuzz, pyarrow; print('deps ok')"
pg_isready                                           # accepting connections
psql -d pex -c "SELECT count(*) FROM information_schema.tables
                WHERE table_schema IN ('public','staging');"   # want 21
uv run python -c "
import os; from dotenv import load_dotenv; load_dotenv()
import psycopg
assert os.environ['PEX_DATA_DIR'], 'PEX_DATA_DIR unset'
with psycopg.connect(os.environ['DATABASE_URL']) as c:
    print('connected as', c.execute('SELECT current_user').fetchone()[0])
"
git check-ignore .env && echo ".env is ignored"
```

The last two are the ones people skip and regret: the first proves the pipeline's own credentials work (not just your superuser account), and the second proves you're not about to commit them.

## Related

- [README.md](README.md) — prerequisites index, credential table, run order
- [data/database/database-setup.md](https://github.com/davidcohn22/physician-expertise-docs/blob/main/data/database/database-setup.md) — the full Postgres walkthrough
