"""Geocode practice addresses with the free US Census batch geocoder.

Coordinates are what make "find a doctor within 25 miles" work — they feed the
GiST index on `ll_to_earth(practice_lat, practice_lng)`.

Two design choices carry most of the weight here:

**Geocode addresses, not physicians.** Results are cached in
`staging.geocode_cache` keyed on a normalized address, so a large clinic with 200
doctors costs one lookup rather than 200, and a *changed* address is
automatically a cache miss while an unchanged one is never re-sent. That
implements "only geocode new or address-changed physicians" without needing to
diff anything: "coordinates for an unchanged address never go stale, and
re-geocoding 1M addresses on every load is hours of wasted API traffic"
(docs/prerequisites/01-nppes-identity.md).

**Unmatched addresses fall back to the ZIP centroid**, flagged as
`practice_geo_precision = 'zip_centroid'` so the UI can say "approximate". PO
boxes and suite-only addresses are the usual cause; coarse coordinates are fine
for radius search, silently missing ones are not.
"""

from __future__ import annotations

import csv
import io
import logging
import re

import psycopg

from ...config import Settings
from ...db import upsert

log = logging.getLogger("pex.nppes.geocode")

ENDPOINT = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
BENCHMARK = "Public_AR_Current"
BATCH_LIMIT = 10_000  # hard API maximum per request

# Census Gazetteer ZCTA file — the ZIP-centroid source for the fallback. Free,
# no key. The year is resolved at runtime because a new file lands each autumn.
GAZETTEER = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{year}_Gazetteer/{year}_Gaz_zcta_national.zip"

# Addresses to skip entirely: the geocoder cannot resolve a PO box, and sending
# them wastes a slot in a 10,000-row batch.
_PO_BOX = re.compile(r"\b(p\.?\s*o\.?\s*box|post office box)\b", re.I)


# ---------------------------------------------------------------------------
# address keys
# ---------------------------------------------------------------------------

ADDRESS_KEY_SQL = """
    md5(
        lower(coalesce(btrim(practice_address_line1), '')) || '|' ||
        lower(coalesce(btrim(practice_city), '')) || '|' ||
        upper(coalesce(btrim(practice_state), '')) || '|' ||
        left(coalesce(btrim(practice_zip), ''), 5)
    )
"""

PENDING_ADDRESSES = f"""
SELECT DISTINCT
    {ADDRESS_KEY_SQL} AS address_key,
    btrim(p.practice_address_line1),
    btrim(p.practice_city),
    upper(btrim(p.practice_state)),
    left(btrim(p.practice_zip), 5)
FROM physician p
LEFT JOIN staging.geocode_cache g ON g.address_key = {ADDRESS_KEY_SQL}
WHERE p.nppes_status = 'active'
  AND p.practice_address_line1 IS NOT NULL
  AND p.practice_state IS NOT NULL
  AND g.address_key IS NULL
"""

APPLY_CACHE = f"""
UPDATE physician p
SET practice_lat = g.lat,
    practice_lng = g.lng,
    practice_geo_precision = g.precision
FROM staging.geocode_cache g
WHERE g.address_key = {ADDRESS_KEY_SQL}
  AND g.lat IS NOT NULL
  AND (p.practice_lat IS DISTINCT FROM g.lat OR p.practice_lng IS DISTINCT FROM g.lng)
"""

UPSERT_CACHE = """
INSERT INTO staging.geocode_cache (address_key, lat, lng, precision, matched_address, retrieved_at)
VALUES (%(address_key)s, %(lat)s, %(lng)s, %(precision)s, %(matched_address)s, now())
ON CONFLICT (address_key) DO UPDATE SET
    lat = EXCLUDED.lat,
    lng = EXCLUDED.lng,
    precision = EXCLUDED.precision,
    matched_address = EXCLUDED.matched_address,
    retrieved_at = EXCLUDED.retrieved_at
"""


# ---------------------------------------------------------------------------
# the batch call
# ---------------------------------------------------------------------------

def geocode_batch(http, rows: list[tuple], settings: Settings, batch_no: int) -> dict[str, tuple]:
    """POST up to 10,000 addresses, return {address_key: (lat, lng, matched)}.

    Both the request and the response CSV are kept in `$PEX_DATA_DIR/geocode/`,
    so a batch that fails can be retried without rebuilding it — and so a
    surprising result can be traced back to exactly what was sent.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    for key, street, city, state, zip5 in rows:
        writer.writerow([key, street or "", city or "", state or "", zip5 or ""])
    payload = buf.getvalue()

    request_path = settings.path("geocode", f"request_{batch_no:04d}.csv")
    request_path.write_text(payload, encoding="utf-8")

    resp = http.post(
        ENDPOINT,
        files={"addressFile": ("batch.csv", payload, "text/csv")},
        data={"benchmark": BENCHMARK},
        timeout=900,
    )
    settings.path("geocode", f"response_{batch_no:04d}.csv").write_text(
        resp.text, encoding="utf-8"
    )

    results = {}
    for line in csv.reader(io.StringIO(resp.text)):
        # id, input address, match flag, exact/non-exact, matched address, "lng,lat", …
        if len(line) < 6 or line[2] != "Match":
            continue
        try:
            lng, lat = line[5].split(",")
            results[line[0]] = (float(lat), float(lng), line[4])
        except (ValueError, IndexError):
            continue
    return results


def run(conn: psycopg.Connection, settings: Settings, http, run_summary=None,
        max_batches: int | None = None) -> dict:
    """Geocode every practice address not already in the cache, then apply it."""
    with conn.cursor() as cur:
        cur.execute(PENDING_ADDRESSES)
        pending = cur.fetchall()

    log.info("%s distinct addresses need geocoding", f"{len(pending):,}")
    stats = {"addresses_pending": len(pending), "rooftop": 0, "zip_centroid": 0, "unresolved": 0}
    if not pending:
        _apply(conn, stats)
        return stats

    # PO boxes never resolve; route them straight to the ZIP fallback.
    sendable = [r for r in pending if not _PO_BOX.search(r[1] or "")]
    skipped = [r for r in pending if _PO_BOX.search(r[1] or "")]

    cached: list[dict] = []
    batches = [sendable[i:i + BATCH_LIMIT] for i in range(0, len(sendable), BATCH_LIMIT)]
    if max_batches:
        batches = batches[:max_batches]

    for batch_no, batch in enumerate(batches, start=1):
        log.info("geocode batch %d/%d (%s addresses)", batch_no, len(batches), f"{len(batch):,}")
        try:
            matched = geocode_batch(http, batch, settings, batch_no)
        except Exception as exc:
            # A failed batch is not a failed load: its addresses simply stay
            # uncached and are retried on the next run.
            log.error("geocode batch %d failed: %s", batch_no, exc)
            if run_summary is not None:
                run_summary.reject("geocode_batch_failed", len(batch))
            continue

        for key, street, city, state, zip5 in batch:
            if key in matched:
                lat, lng, matched_address = matched[key]
                cached.append({
                    "address_key": key, "lat": lat, "lng": lng,
                    "precision": "rooftop", "matched_address": matched_address,
                })
                stats["rooftop"] += 1
            else:
                skipped.append((key, street, city, state, zip5))

    # ZIP-centroid fallback for everything the geocoder could not match.
    centroids = _zip_centroids(conn, {r[4] for r in skipped if r[4]})
    for key, _street, _city, _state, zip5 in skipped:
        point = centroids.get(zip5)
        if point is None:
            stats["unresolved"] += 1
            if run_summary is not None:
                run_summary.reject("no_geocode_no_centroid")
            continue
        cached.append({
            "address_key": key, "lat": point[0], "lng": point[1],
            "precision": "zip_centroid", "matched_address": None,
        })
        stats["zip_centroid"] += 1

    written = upsert(conn, UPSERT_CACHE, cached)
    log.info("cached %s geocodes (%s rooftop, %s zip-centroid, %s unresolved)",
             f"{written:,}", f"{stats['rooftop']:,}", f"{stats['zip_centroid']:,}",
             f"{stats['unresolved']:,}")

    if stats["rooftop"] and stats["rooftop"] / max(len(sendable), 1) < 0.90:
        log.warning(
            "rooftop match rate %.1f%% — the prerequisites doc expects >90%%",
            100 * stats["rooftop"] / max(len(sendable), 1),
        )

    _apply(conn, stats)
    return stats


def _apply(conn: psycopg.Connection, stats: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(APPLY_CACHE)
        stats["physicians_located"] = cur.rowcount
    conn.commit()
    log.info("applied coordinates to %s physician rows", f"{stats['physicians_located']:,}")


# ---------------------------------------------------------------------------
# ZIP centroids
# ---------------------------------------------------------------------------

def _zip_centroids(conn: psycopg.Connection, zips: set[str]) -> dict[str, tuple]:
    if not zips:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT zip5, lat, lng FROM staging.zip_centroid WHERE zip5 = ANY(%s)",
            (sorted(zips),),
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def load_zip_centroids(conn: psycopg.Connection, settings: Settings, http) -> int:
    """Download and load the Census Gazetteer ZCTA file (ZIP → centroid).

    Only needed once; skipped when the table is already populated. The year is
    resolved by trying the current year and walking backwards, since a new file
    lands each autumn and hardcoding one guarantees a future 404.
    """
    from datetime import date
    import zipfile

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM staging.zip_centroid")
        if cur.fetchone()[0] > 30_000:
            log.info("zip centroids already loaded")
            return 0

    path = None
    for year in range(date.today().year, date.today().year - 4, -1):
        url = GAZETTEER.format(year=year)
        try:
            size = http.remote_size(url)
            if not size:
                continue
            path = http.download(url, settings.path("geocode", f"gaz_zcta_{year}.zip"), size)
            break
        except Exception:
            continue
    if path is None:
        log.error("could not resolve a Census Gazetteer ZCTA file; ZIP fallback unavailable")
        return 0

    rows = []
    with zipfile.ZipFile(path) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".txt"))
        with archive.open(name) as fh:
            reader = csv.DictReader(io.TextIOWrapper(fh, encoding="latin-1"), delimiter="\t")
            for raw in reader:
                clean = { (k or "").strip().upper(): (v or "").strip() for k, v in raw.items() }
                zip5, lat, lng = clean.get("GEOID"), clean.get("INTPTLAT"), clean.get("INTPTLONG")
                if not (zip5 and lat and lng):
                    continue
                try:
                    rows.append({"zip5": zip5, "lat": float(lat), "lng": float(lng)})
                except ValueError:
                    continue

    written = upsert(
        conn,
        """
        INSERT INTO staging.zip_centroid (zip5, lat, lng)
        VALUES (%(zip5)s, %(lat)s, %(lng)s)
        ON CONFLICT (zip5) DO UPDATE SET lat = EXCLUDED.lat, lng = EXCLUDED.lng
        """,
        rows,
    )
    log.info("loaded %s ZIP centroids", f"{written:,}")
    return written
