"""ORCID employment confirmation, cached.

An ORCID record's employment history names the institutions a researcher has
actually worked at. When one of those sits in the physician's practice state, the
identity link stops being a name-similarity guess and becomes close to certain —
which is why a confirmation floors the score at 0.97 rather than adding to it.

Caching is not optional here. "Called only when a candidate has an ORCID. **Cache
results** — the same ORCID gets checked repeatedly across matcher re-runs"
(docs/prerequisites/03-publications.md). The matcher is designed to be re-run
freely while its weights are tuned; without a cache, every re-run would re-issue
the same thousands of calls against a free public API that publishes no rate
limit and deserves not to be tested on it.
"""

from __future__ import annotations

import logging

import psycopg

from ...config import Settings
from ...db import Jsonb
from ...http import client_for
from ...us_states import to_code

log = logging.getLogger("pex.orcid")

BASE = "https://pub.orcid.org/v3.0"


class OrcidChecker:
    """Confirms (or fails to confirm) that an ORCID's employer matches a state.

    Cache semantics matter: a *negative* result is cached too. An ORCID with no
    US employment record will never acquire one mid-run, and re-asking on every
    matcher pass would be the bulk of the traffic.
    """

    def __init__(self, conn: psycopg.Connection, settings: Settings):
        self.conn = conn
        self.http = client_for(settings, "orcid")
        self.hits = 0
        self.calls = 0

    def _cached(self, orcid: str) -> list | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT states FROM staging.orcid_employment_cache WHERE orcid = %s", (orcid,)
            )
            row = cur.fetchone()
        return row[0] if row else None

    def _store(self, orcid: str, states: list[str], organizations: list[str]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO staging.orcid_employment_cache (orcid, states, organizations, fetched_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (orcid) DO UPDATE SET
                    states = EXCLUDED.states,
                    organizations = EXCLUDED.organizations,
                    fetched_at = now()
                """,
                (orcid, Jsonb(states), Jsonb(organizations)),
            )
        self.conn.commit()

    def employment_states(self, orcid: str) -> list[str]:
        """Two-letter US states from an ORCID's employment records."""
        cached = self._cached(orcid)
        if cached is not None:
            self.hits += 1
            return cached

        states: list[str] = []
        organizations: list[str] = []
        try:
            # The public API returns XML unless JSON is explicitly requested.
            data = self.http.get(
                f"{BASE}/{orcid}/employments",
                headers={"Accept": "application/json"},
            ).json()
            self.calls += 1
        except Exception as exc:
            # A failed lookup must not fail the match: the pair simply keeps its
            # unconfirmed score. Not cached, so it is retried next run.
            log.debug("ORCID %s lookup failed: %s", orcid, exc)
            return []

        for group in data.get("affiliation-group", []) or []:
            for summary in group.get("summaries", []) or []:
                employment = summary.get("employment-summary") or {}
                organization = employment.get("organization") or {}
                address = organization.get("address") or {}
                if organization.get("name"):
                    organizations.append(organization["name"])
                if (address.get("country") or "").upper() != "US":
                    continue
                code = to_code(address.get("region"))
                if code:
                    states.append(code)

        self._store(orcid, sorted(set(states)), organizations[:20])
        return sorted(set(states))

    def confirms(self, orcid: str, practice_state: str | None) -> bool:
        if not orcid or not practice_state:
            return False
        return practice_state.upper() in set(self.employment_states(orcid))
