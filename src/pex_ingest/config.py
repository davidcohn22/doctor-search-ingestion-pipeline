"""Runtime configuration, read from the environment — never hardcoded.

This single indirection is what makes the eventual move to hosted Postgres a
config change rather than a rewrite (docs/prerequisites/README.md#scheduling-how-this-becomes-autonomous).
Every job starts by calling `load_settings()`; nothing below reaches for
`os.environ` again.

Paths follow the layout in docs/prerequisites/README.md#where-files-live: raw
files land in dated snapshot directories under $PEX_DATA_DIR, outside both git
repos, and are never overwritten.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Subdirectories of $PEX_DATA_DIR, created on demand. Mirrors the tree the
# prerequisites docs tell you to mkdir, plus two the docs imply but don't name:
# _cache (HTTP response cache) and _reports (run summaries, QA sheets).
DATA_SUBDIRS = (
    "nppes",
    "cms-dac",
    "openalex/authors",
    "openalex/works",
    "openalex/reference",
    "ctgov",
    "mesh",
    "nucc",
    "geocode",
    "_archive",
    "_cache",
    "_reports",
)


class ConfigError(RuntimeError):
    """A required setting is missing or obviously wrong.

    Raised early and with a pointer to the doc that explains the fix — an
    unattended job that dies on a clear message at second one is far better
    than one that dies on a stack trace an hour in.
    """


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    contact_email: str

    ncbi_api_key: str = ""
    ncbi_email: str = ""
    openalex_api_key: str = ""
    anthropic_api_key: str = ""

    # A NUCC taxonomy CSV supplied by hand. When set, bucket 1 loads this file
    # instead of scraping nucc.org (which served spam in 2026-10).
    nucc_csv: Path | None = None

    # Tuning knobs with sane defaults; override from the environment when a
    # machine or a run needs something different.
    http_cache: bool = True
    batch_size: int = 5_000
    log_level: str = "INFO"

    _created: set = field(default_factory=set, repr=False, compare=False)

    # -- paths ------------------------------------------------------------

    def path(self, *parts: str) -> Path:
        """A path under $PEX_DATA_DIR, with its parent directory created."""
        p = self.data_dir.joinpath(*parts)
        parent = p if not p.suffix else p.parent
        if parent not in self._created:
            parent.mkdir(parents=True, exist_ok=True)
            self._created.add(parent)
        return p

    def snapshot_dir(self, bucket: str, release: str) -> Path:
        """A dated snapshot directory, e.g. nppes/2026-08-full/.

        Dated and never overwritten, so a parsing fix re-reads the file already
        on disk instead of re-downloading 10 GB, and two releases can be diffed.
        """
        return self.path(bucket, release)

    @property
    def cache_dir(self) -> Path:
        return self.path("_cache")

    @property
    def report_dir(self) -> Path:
        return self.path("_reports")

    # -- credential accessors --------------------------------------------
    # Each raises a targeted error rather than letting a bucket fail later with
    # an opaque 401 from the source.

    def require(self, name: str) -> str:
        value = getattr(self, name, "")
        if not value:
            raise ConfigError(
                f"{name.upper()} is required for this job but is unset.\n"
                "See docs/prerequisites/README.md#credentials-and-where-they-go"
            )
        return value

    @property
    def user_agent(self) -> str:
        """Sent on every outbound request.

        Several agencies (NCBI especially) treat anonymous bulk traffic as abuse
        and throttle it harder, so the contact address is not decoration.
        """
        return f"physician-expertise-explorer/0.1 ({self.contact_email})"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _bool_env(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def load_dotenv_file(path: Path | None = None) -> None:
    """Read the repo-root .env into the environment.

    Deliberately hand-rolled and stdlib-only so that bootstrap scripts work
    before `uv sync` has run. Existing environment variables always win, which
    is what lets a scheduled job or CI override a developer's .env.
    """
    path = path or REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def load_settings(require_db: bool = True) -> Settings:
    """Build Settings from the environment, failing loudly on what's missing."""
    load_dotenv_file()

    database_url = _env("DATABASE_URL")
    if require_db and not database_url:
        raise ConfigError(
            "DATABASE_URL is unset. Copy .env.example to .env and fill it in:\n"
            "  DATABASE_URL=postgresql://pex_ingest:PASSWORD@localhost:5432/pex\n"
            "See docs/prerequisites/00-workstation.md"
        )

    raw_dir = _env("PEX_DATA_DIR")
    if not raw_dir:
        raise ConfigError(
            "PEX_DATA_DIR is unset — every job reads its raw files from there.\n"
            "See docs/prerequisites/00-workstation.md#3-the-data-directory"
        )
    data_dir = Path(raw_dir).expanduser().resolve()
    if not data_dir.exists():
        raise ConfigError(
            f"PEX_DATA_DIR points at {data_dir}, which does not exist.\n"
            "Create it with: mkdir -p " + str(data_dir)
        )
    if not os.access(data_dir, os.W_OK):
        raise ConfigError(f"PEX_DATA_DIR ({data_dir}) is not writable.")

    contact = _env("CONTACT_EMAIL")
    if not contact or contact == "you@example.com":
        raise ConfigError(
            "CONTACT_EMAIL must be a real address — it is sent as the HTTP "
            "User-Agent, and several agencies rate-limit anonymous traffic harder.\n"
            "See docs/prerequisites/README.md#credentials-and-where-they-go"
        )

    settings = Settings(
        database_url=database_url,
        data_dir=data_dir,
        contact_email=contact,
        ncbi_api_key=_env("NCBI_API_KEY"),
        ncbi_email=_env("NCBI_EMAIL") or contact,
        # Accept both spellings: the docs repo's .env uses OPEN_ALEX_API_KEY.
        openalex_api_key=_env("OPENALEX_API_KEY") or _env("OPEN_ALEX_API_KEY"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        nucc_csv=Path(_env("PEX_NUCC_CSV")).expanduser() if _env("PEX_NUCC_CSV") else None,
        http_cache=_bool_env("PEX_HTTP_CACHE", True),
        batch_size=int(_env("PEX_BATCH_SIZE", "5000")),
        log_level=_env("PEX_LOG_LEVEL", "INFO").upper(),
    )

    for sub in DATA_SUBDIRS:
        settings.path(*sub.split("/")).mkdir(parents=True, exist_ok=True)

    return settings
