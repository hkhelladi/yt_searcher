"""M3 — Site discovery, shallow crawl, domain age via RDAP.

The YouTube API does not reliably expose the channel "About" links, so the
candidate site URL is derived from text: channel + sampled video descriptions,
filtered to drop social/aggregator hosts, then picked by host frequency.

All HTTP work in this stage is async (httpx) and runs under the orchestrator's
HTTP semaphore.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import httpx

from enrichment.config import (
    CRAWL_PATHS,
    HTTP_CONCURRENCY,
    HTTP_TIMEOUT_S,
    SOCIAL_AND_AGGREGATOR_HOSTS,
    USER_AGENT,
)
from enrichment.patterns import extract_urls, host_of
from enrichment.schema import EnrichedRecord


def _registrable_key(host: str) -> str:
    """Cheap registrable-domain key: last two labels, or last three if the
    second-to-last is a known compound TLD (co.uk, com.au, ...). Good enough
    for bucketing candidate URLs by site; we are not trying to do public-
    suffix-list-perfect parsing here."""
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    compound = {
        "co", "com", "net", "org", "gov", "edu", "ac",
    }
    if parts[-2] in compound and len(parts[-1]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _is_social_or_aggregator(host: str) -> bool:
    if not host:
        return True
    for h in SOCIAL_AND_AGGREGATOR_HOSTS:
        if host == h or host.endswith("." + h):
            return True
    return False


def pick_candidate_site(*texts: str) -> str | None:
    """Walk every URL in the combined text, drop social/aggregator hosts,
    and return the most-frequent registrable-domain URL (full https URL)."""
    counter: Counter[str] = Counter()
    first_url: dict[str, str] = {}
    for text in texts:
        for url in extract_urls(text):
            host = host_of(url)
            if _is_social_or_aggregator(host):
                continue
            key = _registrable_key(host)
            if not key:
                continue
            counter[key] += 1
            first_url.setdefault(key, f"https://{key}")
    if not counter:
        return None
    top_key, _ = counter.most_common(1)[0]
    return first_url[top_key]


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        return await client.get(url, follow_redirects=True, timeout=HTTP_TIMEOUT_S)
    except (httpx.HTTPError, OSError):
        return None


async def resolve_homepage(
    client: httpx.AsyncClient,
    candidate_url: str,
) -> tuple[str | None, str | None, str | None]:
    """Return (final_url, domain, html) for a 2xx homepage, else (None, None, None)."""
    resp = await _get(client, candidate_url)
    if resp is None or not (200 <= resp.status_code < 300):
        return None, None, None
    final_url = str(resp.url)
    parsed = urlparse(final_url)
    domain = (parsed.hostname or "").lower().removeprefix("www.")
    return final_url, domain, resp.text


async def crawl_paths(
    client: httpx.AsyncClient,
    base_url: str,
    paths: list[str],
) -> dict[str, str]:
    """Fetch a small set of paths off the resolved homepage, returning {path: html}.
    404s and errors are silently skipped — these are best-effort discovery probes."""
    out: dict[str, str] = {}

    async def one(path: str) -> None:
        if path == "/":
            return
        url = urljoin(base_url, path)
        resp = await _get(client, url)
        if resp is not None and 200 <= resp.status_code < 300 and resp.text:
            out[path] = resp.text

    await asyncio.gather(*(one(p) for p in paths))
    return out


def _parse_rdap_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


async def domain_age_rdap(
    client: httpx.AsyncClient,
    domain: str,
) -> date | None:
    """Query rdap.org for the registration event date. None on redacted/404."""
    if not domain:
        return None
    url = f"https://rdap.org/domain/{domain}"
    resp = await _get(client, url)
    if resp is None or resp.status_code >= 400:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    for ev in data.get("events", []) or []:
        if (ev.get("eventAction") or "").lower() == "registration":
            return _parse_rdap_date(ev.get("eventDate"))
    return None


async def _enrich_one_site(
    client: httpx.AsyncClient,
    record: EnrichedRecord,
    semaphore: asyncio.Semaphore,
) -> EnrichedRecord:
    async with semaphore:
        # 1. Discover candidate URL from descriptions
        candidate = pick_candidate_site(
            record.yt_channel_description,
            *record.yt_video_descriptions,
        )
        record.site_url = candidate
        if not candidate:
            record.site_resolved = False
            if "M3" not in record.stages_completed:
                record.stages_completed.append("M3")
            return record

        # 2. Resolve homepage
        final_url, domain, html = await resolve_homepage(client, candidate)
        if not final_url or not html:
            record.site_resolved = False
            if "M3" not in record.stages_completed:
                record.stages_completed.append("M3")
            return record

        record.site_resolved = True
        record.site_final_url = final_url
        record.domain = domain
        record.site_html_cache["/"] = html

        # 3. Shallow crawl
        crawled = await crawl_paths(client, final_url, CRAWL_PATHS)
        record.site_html_cache.update(crawled)

        # 4. RDAP age
        record.domain_created_at = await domain_age_rdap(client, domain)

    if "M3" not in record.stages_completed:
        record.stages_completed.append("M3")
    return record


async def stage_site(records: list[EnrichedRecord]) -> list[EnrichedRecord]:
    """Discover + crawl + RDAP-age for every record."""
    if not records:
        return records
    sem = asyncio.Semaphore(HTTP_CONCURRENCY)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
    async with httpx.AsyncClient(headers=headers, http2=False) as client:
        return await asyncio.gather(*(_enrich_one_site(client, r, sem) for r in records))
