"""Polite HTTP: one client, used by every bucket.

The pacing rules from docs/prerequisites/README.md#conventions-every-bucket-follows
live here so no bucket has to remember them:

  * a real `User-Agent` carrying CONTACT_EMAIL on every request,
  * a minimum interval between calls, per source, set from that source's
    documented rate limit,
  * retry with backoff on the transient statuses (429, 5xx), honouring
    `Retry-After` when the server sends one,
  * an optional on-disk response cache, which makes re-runs during development
    effectively free.

"There is no published hard quota, which is exactly why staying comfortably
under matters — an unthrottled scraper is how a free public API acquires a rate
limit." — docs/prerequisites/04-clinical-trials.md
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("pex.http")

# Some government landing pages reject a non-browser User-Agent outright. The
# NPPES page returns HTTP 403 without one and 200 with one — "the single most
# likely reason a first automated run fails, and the error looks like the file is
# missing rather than like a rejected request"
# (docs/prerequisites/01-nppes-identity.md).
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpError(RuntimeError):
    pass


class HttpClient:
    """A rate-limited, retrying, optionally caching HTTP client.

    `min_interval` is the pacing knob and is set per source at construction —
    1.3s for ClinicalTrials.gov (~46 req/min, under its ~50/min limit), 0.1s for
    PubMed with an API key (10 req/sec), and so on.
    """

    def __init__(
        self,
        user_agent: str,
        min_interval: float = 0.0,
        cache_dir: Path | None = None,
        timeout: int = 120,
        max_retries: int = 5,
        browser_ua: bool = False,
    ):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = BROWSER_UA if browser_ua else user_agent
        self.min_interval = min_interval
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_call = 0.0
        self.calls = 0
        self.cache_hits = 0
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)

    # -- pacing -----------------------------------------------------------

    def _wait(self) -> None:
        if self.min_interval <= 0:
            return
        gap = time.monotonic() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)

    # -- caching ----------------------------------------------------------

    def _cache_path(self, url: str, params: dict | None) -> Path | None:
        if self.cache_dir is None:
            return None
        key = url + "|" + json.dumps(params or {}, sort_keys=True)
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    # -- requests ---------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._wait()
            try:
                resp = self.session.request(
                    method, url, params=params, timeout=self.timeout, stream=stream, **kwargs
                )
                self._last_call = time.monotonic()
                self.calls += 1

                if resp.status_code in RETRY_STATUSES:
                    delay = self._retry_delay(resp, attempt)
                    log.warning(
                        "%s %s → HTTP %d; retrying in %.1fs (attempt %d/%d)",
                        method, url, resp.status_code, delay, attempt + 1, self.max_retries,
                    )
                    time.sleep(delay)
                    last_error = HttpError(f"HTTP {resp.status_code} from {url}")
                    continue

                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                # Connection resets and read timeouts are the normal failure mode
                # of a multi-hour bulk download, not an exceptional one.
                last_error = exc
                delay = min(2**attempt, 60)
                log.warning("%s %s failed (%s); retrying in %.1fs", method, url, exc, delay)
                time.sleep(delay)
                self._last_call = time.monotonic()

        raise HttpError(f"{method} {url} failed after {self.max_retries} attempts: {last_error}")

    @staticmethod
    def _retry_delay(resp: requests.Response, attempt: int) -> float:
        """Honour Retry-After when present; exponential backoff otherwise."""
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return min(float(header), 300.0)
            except ValueError:
                pass
        return float(min(2**attempt, 60))

    def get(self, url: str, params: dict | None = None, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, params=params, **kwargs)

    def get_json(self, url: str, params: dict | None = None, use_cache: bool = True) -> Any:
        cache_path = self._cache_path(url, params) if use_cache else None
        if cache_path is not None and cache_path.exists():
            self.cache_hits += 1
            return json.loads(cache_path.read_text(encoding="utf-8"))

        data = self.get(url, params=params).json()
        if cache_path is not None:
            cache_path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def get_text(self, url: str, params: dict | None = None) -> str:
        return self.get(url, params=params).text

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def head(self, url: str) -> requests.Response:
        return self.request("HEAD", url, allow_redirects=True)

    def remote_size(self, url: str) -> int | None:
        """Content-Length without downloading — the basis of the skip check."""
        try:
            value = self.head(url).headers.get("Content-Length")
            return int(value) if value else None
        except Exception:
            return None

    def download(
        self,
        url: str,
        dest: Path,
        expected_size: int | None = None,
        chunk_size: int = 1 << 20,
    ) -> Path:
        """Download to `dest`, skipping the transfer if it is already complete.

        "Downloaders are content-addressed. Resolve the current file, compare
        against what's already in PEX_DATA_DIR, skip if unchanged. A weekly job
        that re-downloads 10 GB to discover nothing changed is how a schedule
        gets switched off." — docs/prerequisites/README.md

        The size check is what distinguishes a complete file from one truncated
        by an interrupted run; a partial download writes to a `.part` file and is
        only moved into place once the byte count matches, so a crashed transfer
        can never masquerade as a finished one.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        if expected_size is None:
            expected_size = self.remote_size(url)

        if dest.exists():
            local = dest.stat().st_size
            if expected_size is None or local == expected_size:
                log.info("already local, skipping: %s (%s bytes)", dest.name, f"{local:,}")
                return dest
            log.warning(
                "%s is %s bytes but the source reports %s — re-downloading",
                dest.name, f"{local:,}", f"{expected_size:,}",
            )

        part = dest.with_suffix(dest.suffix + ".part")
        log.info("downloading %s → %s", url, dest)
        started = time.monotonic()
        written = 0
        with self.get(url, stream=True) as resp, open(part, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                fh.write(chunk)
                written += len(chunk)
                if written % (200 * chunk_size) == 0:
                    log.info("  … %.1f GB", written / 1e9)

        if expected_size is not None and written != expected_size:
            part.unlink(missing_ok=True)
            raise HttpError(
                f"{url}: downloaded {written:,} bytes, expected {expected_size:,}"
            )

        part.replace(dest)
        rate = written / max(time.monotonic() - started, 0.001) / 1e6
        log.info("downloaded %s (%.2f GB, %.1f MB/s)", dest.name, written / 1e9, rate)
        return dest


def client_for(settings, source: str) -> HttpClient:
    """A client paced for a named source.

    Intervals come straight from each source's documented limit; where a source
    publishes none (ClinicalTrials.gov, ORCID) the value is a deliberate
    politeness choice rather than a measured ceiling.
    """
    intervals = {
        "nppes": 0.5,
        "cms": 0.5,
        "census": 1.0,       # batch geocoder: 10k addresses per call, ~100 calls total
        "ctgov": 1.3,        # ~46 req/min, under the ~50/min limit
        "openalex": 0.15,
        "pubmed": 0.11 if settings.ncbi_api_key else 0.34,  # 10/s with a key, 3/s without
        "orcid": 0.5,
        "nlm": 0.5,
        "s3": 0.0,           # AWS Open Data; throughput-bound, not rate-limited
    }
    cache_dir = settings.cache_dir / source if settings.http_cache else None
    return HttpClient(
        user_agent=settings.user_agent,
        min_interval=intervals.get(source, 0.5),
        cache_dir=cache_dir,
        browser_ua=source in ("nppes", "nucc"),
    )
