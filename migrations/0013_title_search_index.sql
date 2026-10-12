-- 0013 — full-text title search for unknown concepts, and the synonym-kind check
-- (2026-10-11). Apply AFTER the works re-run: building these during it would slow
-- both (the works step is bottlenecked on Postgres disk reads).
--
-- When a search names something the vocabulary doesn't have, the AI step returns
-- tiered search terms and the API counts them live against paper titles
-- (measured 0.3 s warm, 1.6 s cold across 5.7M titles). to_tsvector('english', …)
-- stems words, so "pyeloplasties" matches "pyeloplasty"; the API's queries must use
-- the same expression for the index to apply.
CREATE INDEX IF NOT EXISTS work_title_fts
    ON work USING gin (to_tsvector('english', title));

CREATE INDEX IF NOT EXISTS trial_title_fts
    ON trial USING gin (to_tsvector('english', brief_title || ' ' || coalesce(official_title, '')));

-- 0009 added the new synonym kinds NOT VALID, because the old MeSH-era rows were
-- still there. The vocabulary load has replaced them; check every row now.
ALTER TABLE condition_synonym VALIDATE CONSTRAINT condition_synonym_kind_check;
