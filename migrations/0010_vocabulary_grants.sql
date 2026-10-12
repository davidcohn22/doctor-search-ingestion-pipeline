-- 0010 — let the read-only API role read the new vocabulary tables.
--
-- 08-roles-and-grants.sql gives pex_app SELECT on the tables that existed when it
-- ran; tables added later need an explicit grant. The API reads condition_link
-- (related-search chips, the same-specialty check) and specialty_field;
-- condition_term is build-time only, but read access is harmless and keeps
-- "pex_app can read every public table" true.
GRANT SELECT ON condition_term, condition_link, specialty_field TO pex_app;
