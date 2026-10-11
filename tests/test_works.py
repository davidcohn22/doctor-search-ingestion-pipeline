"""Projecting a snapshot work record into a `work` row."""

import datetime
import json

from pex_ingest.buckets.publications.works import slim_work


def test_parquet_shaped_record_projects_cleanly():
    """Parquet hands back `ids` as [(key, value), …] and dates as datetime.date."""
    record = {
        "id": "https://openalex.org/W2323081207",
        "doi": "https://doi.org/10.1097/aap.0b013e318265f0ae",
        "ids": [("openalex", "https://openalex.org/W2323081207"),
                ("pmid", "https://pubmed.ncbi.nlm.nih.gov/22914657")],
        "title": "International Registries of Regional Anesthesia",
        "publication_year": 2012,
        "publication_date": datetime.date(2012, 1, 1),
        "mesh": [{"descriptor_ui": "D000779"}, {"descriptor_ui": "D000779"}],
    }
    row = slim_work(record)
    assert row["pmid"] == "22914657"
    assert row["doi"] == "10.1097/aap.0b013e318265f0ae"
    assert row["mesh_terms"].obj == ["D000779"]
    json.dumps(row["raw"].obj)  # must be JSON-serializable


def test_concurrent_reads_write_in_manifest_order(monkeypatch):
    """Parts may finish out of order; rows and markers must land in order."""
    import random
    import time

    from pex_ingest.buckets.publications import works

    class Part:
        def __init__(self, name):
            self.name = name

    parts = [Part(f"p{i}") for i in range(12)]
    written = []

    def fake_read(http, part, authors, us_authors, keep_edges):
        time.sleep(random.random() / 100)
        return [], [], [], 1

    monkeypatch.setattr(works, "matched_authors", lambda conn: {"A1": [("1", 0.9)]})
    monkeypatch.setattr(works, "manifest", lambda http, entity: parts)
    monkeypatch.setattr(works, "save_manifest", lambda parts, path: None)
    monkeypatch.setattr(works, "get_marker", lambda conn, job: {"p0"})
    monkeypatch.setattr(works, "add_marker", lambda conn, job, name: written.append(name))
    monkeypatch.setattr(works, "analyze", lambda conn, *tables: None)
    monkeypatch.setattr(works, "read_and_project", fake_read)

    class Settings:
        def path(self, *parts):
            return None

    stats = works.build(None, Settings(), None, keep_edges=False, workers=4)
    assert written == [f"p{i}" for i in range(1, 12)]
    assert stats["records"] == 11
