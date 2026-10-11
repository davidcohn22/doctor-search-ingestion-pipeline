"""OpenAlex reference sets: institutions, topics, sources.

Small (<1 GB combined) and a hard prerequisite for matching:

  "Reference sets loaded first — institutions, sources, topics. The matcher's
   geography scoring resolves institution IDs to city/state through the
   institutions table, so it must exist before scoring runs."
   — docs/prerequisites/03-publications.md

An author record only names its affiliations by institution ID. Without this
lookup, "does this author work where this doctor practices?" — one of the two
strongest signals available — cannot be asked at all, and the matcher silently
degrades to scoring on names alone.

These land in `staging` because they are matcher inputs, not product data. The
serving schema has no institution table and needs none: `work.openalex_source_id`
already carries the journal identifier the application would join on.
"""

from __future__ import annotations

import logging

import psycopg

from ...config import Settings
from ...db import analyze, upsert
from ...us_states import to_code
from .snapshot import manifest, openalex_id, read_part

log = logging.getLogger("pex.openalex.reference")

INSTITUTION_COLUMNS = ["id", "display_name", "country_code", "geo", "type", "ror"]
TOPIC_COLUMNS = ["id", "display_name", "subfield", "field", "domain"]
SOURCE_COLUMNS = ["id", "display_name", "issn_l", "type", "host_organization_name"]

UPSERT_INSTITUTION = """
INSERT INTO staging.openalex_institution
    (openalex_institution_id, display_name, country_code, city, region, state_code, type)
VALUES (%(id)s, %(display_name)s, %(country_code)s, %(city)s, %(region)s, %(state_code)s, %(type)s)
ON CONFLICT (openalex_institution_id) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    country_code = EXCLUDED.country_code,
    city         = EXCLUDED.city,
    region       = EXCLUDED.region,
    state_code   = EXCLUDED.state_code,
    type         = EXCLUDED.type
"""

UPSERT_TOPIC = """
INSERT INTO staging.openalex_topic
    (openalex_topic_id, display_name, subfield_id, subfield_name, field_name, domain_name)
VALUES (%(id)s, %(display_name)s, %(subfield_id)s, %(subfield_name)s,
        %(field_name)s, %(domain_name)s)
ON CONFLICT (openalex_topic_id) DO UPDATE SET
    display_name  = EXCLUDED.display_name,
    subfield_id   = EXCLUDED.subfield_id,
    subfield_name = EXCLUDED.subfield_name,
    field_name    = EXCLUDED.field_name,
    domain_name   = EXCLUDED.domain_name
"""

UPSERT_SOURCE = """
INSERT INTO staging.openalex_source
    (openalex_source_id, display_name, issn_l, type, host_organization)
VALUES (%(id)s, %(display_name)s, %(issn_l)s, %(type)s, %(host)s)
ON CONFLICT (openalex_source_id) DO UPDATE SET
    display_name      = EXCLUDED.display_name,
    issn_l            = EXCLUDED.issn_l,
    type              = EXCLUDED.type,
    host_organization = EXCLUDED.host_organization
"""


def _institution_row(record: dict) -> dict | None:
    ident = openalex_id(record.get("id"))
    if not ident:
        return None
    geo = record.get("geo") or {}
    region = geo.get("region")
    return {
        "id": ident,
        "display_name": record.get("display_name"),
        "country_code": geo.get("country_code") or record.get("country_code"),
        "city": geo.get("city"),
        "region": region,
        # Pre-resolved to a two-letter code so the matcher compares codes to
        # codes rather than re-parsing 'Ohio' on every candidate pair.
        "state_code": to_code(region),
        "type": record.get("type"),
    }


def _topic_row(record: dict) -> dict | None:
    ident = openalex_id(record.get("id"))
    if not ident:
        return None
    subfield = record.get("subfield") or {}
    return {
        "id": ident,
        "display_name": record.get("display_name"),
        "subfield_id": openalex_id(subfield.get("id")),
        "subfield_name": subfield.get("display_name"),
        "field_name": (record.get("field") or {}).get("display_name"),
        "domain_name": (record.get("domain") or {}).get("display_name"),
    }


def _source_row(record: dict) -> dict | None:
    ident = openalex_id(record.get("id"))
    if not ident:
        return None
    return {
        "id": ident,
        "display_name": record.get("display_name"),
        "issn_l": record.get("issn_l"),
        "type": record.get("type"),
        "host": record.get("host_organization_name"),
    }


ENTITIES = {
    "institutions": (INSTITUTION_COLUMNS, _institution_row, UPSERT_INSTITUTION),
    "topics": (TOPIC_COLUMNS, _topic_row, UPSERT_TOPIC),
    "sources": (SOURCE_COLUMNS, _source_row, UPSERT_SOURCE),
}


def load(conn: psycopg.Connection, settings: Settings, http, entities=None) -> dict:
    """Load the reference sets. Small enough to cache locally and re-read."""
    stats = {}
    for entity in entities or ENTITIES:
        columns, mapper, statement = ENTITIES[entity]
        parts = manifest(http, entity)
        # One directory per entity: part file names (updated_date=…__part_0000)
        # repeat across entities and would overwrite each other in a shared one.
        local_dir = settings.path("openalex", "reference", entity)

        def rows():
            for part in parts:
                for record in read_part(http, part, columns, local_dir=local_dir):
                    row = mapper(record)
                    if row is not None:
                        yield row

        written = upsert(conn, statement, rows())
        stats[entity] = written
        log.info("%s: %s rows", entity, f"{written:,}")

    if "institutions" in (entities or ENTITIES):
        stats["institution_states_inferred"] = infer_missing_states(conn)

    analyze(
        conn,
        "staging.openalex_institution",
        "staging.openalex_topic",
        "staging.openalex_source",
    )
    return stats


# ~10% of US institutions have no region in OpenAlex (Johns Hopkins Hospital and
# Mayo Clinic Arizona among them), which blanks the matcher's geography signal
# for some of the largest academic centres. Fill the state from the city when
# NPPES practice addresses put that city name overwhelmingly in one state.
# Measured 2026-10-10: agrees with OpenAlex on 99.3% of institutions that do
# have a region; fills ~2,200 of ~3,300 gaps. Ambiguous names (Portland) stay
# NULL. Inferred rows are recognisable as state_code set with region NULL.
INFER_STATES = """
WITH city_state AS (
    SELECT upper(practice_city) AS city, practice_state AS st, count(*) AS n,
           sum(count(*)) OVER (PARTITION BY upper(practice_city)) AS total
    FROM physician
    WHERE practice_city IS NOT NULL
    GROUP BY 1, 2
), dominant AS (
    SELECT city, st FROM city_state WHERE n >= 0.95 * total AND total >= 5
)
UPDATE staging.openalex_institution i
SET state_code = d.st
FROM dominant d
WHERE i.country_code = 'US'
  AND i.state_code IS NULL
  AND nullif(i.region, '') IS NULL
  AND d.city = upper(i.city)
"""


def infer_missing_states(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(INFER_STATES)
        filled = cur.rowcount
    conn.commit()
    log.info("institution states inferred from city: %s", f"{filled:,}")
    return filled


def us_institution_states(conn: psycopg.Connection) -> dict[str, str]:
    """{institution_id: 'OH'} for US institutions — the matcher's geo lookup.

    Held in memory during matching (a few hundred thousand small entries) because
    the alternative is a database round-trip per candidate affiliation, and the
    matcher scores millions of pairs.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT openalex_institution_id, state_code FROM staging.openalex_institution "
            "WHERE state_code IS NOT NULL"
        )
        return dict(cur.fetchall())


def us_institution_ids(conn: psycopg.Connection) -> set[str]:
    """Every US institution, with or without a state — the author-pool filter.

    Being US-affiliated is what puts an author in the pool; the state only feeds
    the geography score later, so a missing state must not drop the author.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT openalex_institution_id FROM staging.openalex_institution "
            "WHERE country_code = 'US'"
        )
        return {row[0] for row in cur.fetchall()}


def topic_subfields(conn: psycopg.Connection) -> dict[str, str]:
    """{topic_id: subfield_id} — used to turn an author's topics into a specialty."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT openalex_topic_id, subfield_id FROM staging.openalex_topic "
            "WHERE subfield_id IS NOT NULL"
        )
        return dict(cur.fetchall())
