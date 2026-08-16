-- 0005 — geocoding caches.
--
-- Geocode ADDRESSES, not physicians. A 200-doctor clinic is one Census lookup
-- rather than 200, an unchanged address is never re-sent, and a changed address
-- is automatically a cache miss. That implements "only geocode new or
-- address-changed physicians" (docs/prerequisites/01-nppes-identity.md) without
-- diffing anything.

CREATE TABLE IF NOT EXISTS staging.geocode_cache (
    address_key     text PRIMARY KEY,   -- md5 of the normalized practice address
    lat             double precision,
    lng             double precision,
    precision       text CHECK (precision IN ('rooftop', 'zip_centroid')),
    matched_address text,
    retrieved_at    timestamptz NOT NULL DEFAULT now()
);

-- ZIP centroids from the Census Gazetteer, for addresses the geocoder cannot
-- match (PO boxes, suite-only addresses). Coarse but fine for radius search, and
-- flagged as 'zip_centroid' so the UI can say "approximate". The DDL has the
-- practice_geo_precision flag but nothing to compute the fallback from.
CREATE TABLE IF NOT EXISTS staging.zip_centroid (
    zip5 text PRIMARY KEY,
    lat  double precision NOT NULL,
    lng  double precision NOT NULL
);
