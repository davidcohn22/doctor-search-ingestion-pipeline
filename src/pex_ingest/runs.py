"""Run summaries and logging setup.

"Every job logs a run summary — rows in, rows written, rows rejected, elapsed.
Unattended jobs you can't inspect afterwards are worse than no schedule at all,
because a silent failure looks exactly like success."
  — docs/prerequisites/README.md#scheduling-how-this-becomes-autonomous

A RunSummary is both the log line and the durable record: on exit it writes its
counters into `staging.ingest_state.notes` for the job, so "what did last
Tuesday's NPPES run actually do?" is a SQL query rather than a hunt through
terminal scrollback.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("pex")


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """Configure root logging once, to stderr and optionally to a dated file.

    Scheduled runs (launchd, later GitHub Actions) capture stderr; the file copy
    is what makes "compare this week's run to last week's" possible.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        handlers.append(logging.FileHandler(log_dir / f"pex-{stamp}.log", encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # These libraries are chatty at INFO and say nothing we need.
    for noisy in ("urllib3", "httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@dataclass
class RunSummary:
    """Counters for one job, written to the log and to ingest_state on exit.

    Use as a context manager:

        with RunSummary("nppes_seed", conn) as run:
            run.add("read", n)
            run.reject("bad_grad_year", 3)
    """

    job: str
    conn: Any = None  # psycopg connection; None keeps the summary log-only
    counters: Counter = field(default_factory=Counter)
    rejects: Counter = field(default_factory=Counter)
    extra: dict = field(default_factory=dict)
    report_path: Path | None = None
    started_at: float = field(default_factory=time.monotonic)
    failed: bool = False

    def add(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def reject(self, reason: str, n: int = 1) -> None:
        """Count a row we deliberately refused.

        Rejects are a first-class number, not an error: a load with zero
        rejects usually means the guard isn't running, and a load with a
        sudden spike means the source changed shape.
        """
        self.rejects[reason] += n
        self.counters["rejected"] += n

    def note(self, key: str, value: Any) -> None:
        self.extra[key] = value

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def as_dict(self) -> dict:
        return {
            "job": self.job,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_sec": round(self.elapsed, 1),
            "counters": dict(self.counters),
            "rejects": dict(self.rejects),
            "failed": self.failed,
            **self.extra,
        }

    def __enter__(self) -> RunSummary:
        log.info("▶ %s starting", self.job)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.failed = exc_type is not None
        summary = self.as_dict()

        parts = [f"{k}={v:,}" if isinstance(v, int) else f"{k}={v}"
                 for k, v in self.counters.items()]
        line = "  ".join(parts) or "no counters"
        if self.failed:
            log.error("✖ %s FAILED after %.1fs | %s", self.job, self.elapsed, line)
        else:
            log.info("✔ %s finished in %.1fs | %s", self.job, self.elapsed, line)
        if self.rejects:
            log.warning("  rejects: %s", dict(self.rejects))

        # Persist even on failure — a failed run's counters are exactly what you
        # want to see when working out how far it got.
        if self.conn is not None:
            try:
                from .state import record_run  # local import: avoids a cycle

                record_run(self.conn, self.job, summary)
            except Exception:  # never let bookkeeping mask the real error
                log.exception("could not persist run summary for %s", self.job)

        if self.report_path is not None:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(
                json.dumps(summary, indent=2, default=str), encoding="utf-8"
            )
        return False  # never swallow the exception
