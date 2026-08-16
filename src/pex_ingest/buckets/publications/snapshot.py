"""Read the OpenAlex Parquet snapshot, projected to the columns we need.

This module implements the decision in
docs/decisions/0001-openalex-works-via-snapshot.md, which is the single largest
timeline lever in the project: it turns bucket 3 from "2–5 months, or $150" into
"a few hours, $0".

**The trick, and why it works.** Parquet is a columnar format: each column lives
in its own contiguous block and a footer records where each block starts. So a
reader can fetch *only the columns it wants*, as HTTP range requests, instead of
streaming whole records and discarding 90% of each one. Measured on a real works
part file:

    remote file size: 98.3 MB
    PROJECTED    rows=84,037  wire=12.2 MB (12.4% of file)  24 range requests
    ALL columns  rows=84,037  wire=98.2 MB (99.9% of file)  190 range requests

Two-fifths of the works corpus is `abstract_inverted_index`, which this product
does not use, and another eighth is the deprecated `concepts` field. The columns
we need are a thin slice — 725 GB becomes ~90 GB.

Access is anonymous: no AWS account, no requester-pays, no key. The AWS Open Data
program covers the egress.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("pex.openalex.snapshot")

S3_HTTPS = "https://openalex.s3.amazonaws.com/"
PARQUET_ROOT = S3_HTTPS + "data/parquet/"

# The projection. Everything omitted here is omitted deliberately — see the
# decision doc's per-column breakdown for what each omission saves.
WORK_COLUMNS = [
    "id",
    "doi",
    "ids",
    "title",
    "publication_year",
    "publication_date",
    "type",
    "cited_by_count",
    "authorships",
    "primary_location",
    "topics",
    "mesh",
]

AUTHOR_COLUMNS = [
    "id",
    "display_name",
    "display_name_alternatives",
    "orcid",
    "affiliations",
    "last_known_institutions",
    "topics",
    "works_count",
    "cited_by_count",
]


@dataclass(frozen=True)
class PartFile:
    """One part file from a snapshot manifest."""

    url: str
    record_count: int
    content_length: int

    @property
    def name(self) -> str:
        """A stable local name, e.g. 'updated_date=2026-06-26__part_000.parquet'."""
        return "__".join(self.url.rstrip("/").split("/")[-2:])


def manifest(http, entity: str) -> list[PartFile]:
    """Every part file for an entity, from the public manifest.

    Start from `manifest.json` rather than listing the bucket: it gives every
    file path, its record count and its byte length, so a multi-hour download can
    be planned and resumed deterministically — and so a truncated local file is
    detectable rather than merely suspected.
    """
    url = f"{PARQUET_ROOT}{entity}/manifest.json"
    data = http.get_json(url, use_cache=True)

    entries = data.get("files") or data.get("entries") or []
    parts = []
    for entry in entries:
        if isinstance(entry, str):
            parts.append(PartFile(_to_https(entry), 0, 0))
            continue
        location = entry.get("url") or entry.get("key") or entry.get("path") or ""
        meta = entry.get("meta") or entry
        parts.append(
            PartFile(
                url=_to_https(location),
                record_count=int(meta.get("record_count") or 0),
                content_length=int(meta.get("content_length") or entry.get("size") or 0),
            )
        )

    total_bytes = sum(p.content_length for p in parts)
    log.info(
        "%s manifest: %s part files, %s records, %.1f GB (snapshot %s)",
        entity, f"{len(parts):,}",
        f"{data.get('record_count', 0):,}",
        total_bytes / 1e9,
        data.get("date", "unknown"),
    )
    return parts


def _to_https(location: str) -> str:
    if location.startswith("s3://openalex/"):
        return S3_HTTPS + location[len("s3://openalex/"):]
    if location.startswith("http"):
        return location
    return S3_HTTPS + location.lstrip("/")


class HttpRangeFile(io.RawIOBase):
    """A seekable read-only file backed by HTTP Range requests.

    This is what lets pyarrow read a remote Parquet file's footer, decide which
    byte ranges hold the requested columns, and fetch only those — the mechanism
    behind the 12.4% figure above. Without it, "projection" would only reduce
    bytes *parsed*, not bytes *transferred*, and the download would still be
    725 GB.
    """

    def __init__(self, http, url: str, size: int | None = None):
        self._http = http
        self._url = url
        self._pos = 0
        self._size = size or http.remote_size(url) or 0
        self.bytes_read = 0
        self.requests = 0

    # -- io.RawIOBase contract -------------------------------------------

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._size + offset
        return self._pos

    def tell(self) -> int:
        return self._pos

    def readinto(self, buffer) -> int:
        wanted = len(buffer)
        if wanted == 0 or self._pos >= self._size:
            return 0
        end = min(self._pos + wanted - 1, self._size - 1)
        resp = self._http.get(self._url, headers={"Range": f"bytes={self._pos}-{end}"})
        chunk = resp.content
        buffer[: len(chunk)] = chunk
        self._pos += len(chunk)
        self.bytes_read += len(chunk)
        self.requests += 1
        return len(chunk)

    @property
    def size(self) -> int:
        return self._size


def read_part(
    http,
    part: PartFile,
    columns: list[str],
    local_dir: Path | None = None,
    batch_size: int = 20_000,
) -> Iterator[dict]:
    """Stream one part file's rows as dicts, reading only `columns`.

    Two modes, and the choice matters at different scales:

      * **Remote projection** (default) — pyarrow reads the footer and pulls only
        the requested column blocks over HTTP ranges. Right for the 725 GB works
        corpus, where the projected slice is ~12% of the bytes.
      * **Local cache** (`local_dir` given) — the whole part file is downloaded
        first, then read. Right for anything re-read repeatedly, and for the
        reference sets, which are small.
    """
    import pyarrow.parquet as pq

    if local_dir is not None:
        path = local_dir / part.name
        http.download(part.url, path, part.content_length or None)
        source = pq.ParquetFile(path)
    else:
        handle = io.BufferedReader(
            HttpRangeFile(http, part.url, part.content_length or None), buffer_size=1 << 20
        )
        source = pq.ParquetFile(handle)

    available = set(source.schema_arrow.names)
    wanted = [c for c in columns if c in available]
    missing = [c for c in columns if c not in available]
    if missing:
        # A renamed or dropped upstream column would otherwise show up much later
        # as unexplained empty fields on real profiles.
        log.warning("part %s is missing expected columns: %s", part.name, missing)

    for batch in source.iter_batches(batch_size=batch_size, columns=wanted):
        yield from batch.to_pylist()


def openalex_id(value) -> str | None:
    """'https://openalex.org/A5068285584' → 'A5068285584'."""
    if not value:
        return None
    if isinstance(value, dict):
        value = value.get("id") or value.get("openalex")
    if not value:
        return None
    return str(value).rstrip("/").rsplit("/", 1)[-1] or None


def save_manifest(parts: list[PartFile], path: Path) -> None:
    """Keep the manifest locally so a resumed run does not depend on the network."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{"url": p.url, "records": p.record_count, "bytes": p.content_length}
                    for p in parts], indent=1),
        encoding="utf-8",
    )
