"""M3 — Site discovery, shallow crawl, domain age via RDAP.

The YouTube API does not reliably expose the channel "About" links, so the
candidate site URL is derived from text: channel + sampled video descriptions,
filtered to drop social/aggregator hosts, then picked by host frequency.

All HTTP work in this stage is async (httpx) and runs under the orchestrator's
HTTP semaphore.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from datetime import date, datetime
from urllib.parse import unquote, urljoin, urlparse

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


def _channel_name_token(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def _longest_common_substring_len(a: str, b: str) -> int:
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    # Rolling DP to stay O(min(m,n)) memory.
    prev = [0] * (n + 1)
    best = 0
    for i in range(1, m + 1):
        curr = [0] * (n + 1)
        ai = a[i - 1]
        for j in range(1, n + 1):
            if ai == b[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > best:
                    best = curr[j]
        prev = curr
    return best


def _name_overlap(channel_token: str, domain_key: str) -> bool:
    """True if the channel-title token and the domain's primary label share a
    run of ≥5 consecutive characters. Beats full-substring matching on cases
    where neither side fully contains the other (e.g. channel "Romain Faure"
    + domain "itsromain.com" share "romain")."""
    if not channel_token or not domain_key:
        return False
    dom_token = domain_key.split(".", 1)[0]
    return _longest_common_substring_len(channel_token, dom_token) >= 5


def pick_candidate_site(
    weighted_sources: list[tuple[str, float]],
    channel_title: str = "",
) -> str | None:
    """Pick the creator's likely own site.

    Three-tier decision:
      A. **Aggregator-subdomain rule** — if any URL lives on a blocked
         registrable (carrd.co, home.blog, …) but the subdomain prefix
         name-matches the channel title (e.g. `pinoypersonalfinance.carrd.co`
         + channel "Pinoy.PersonalFinance"), return that full URL. The
         creator has no real site, but their hosted page is the best we have.
      B. **Name-matched registrable** — among non-blocked domains, prefer
         any whose primary label name-matches the channel title (e.g.
         "EveryDollar" → `everydollar.com`). Tie-break by weighted frequency.
      C. **Weighted frequency leader** — fall back to the most-mentioned
         non-blocked domain across all sources.

    Each URL is deduped per source so repeated mentions inside one video
    description don't artificially inflate the count.
    """
    # Tokenize each source separately so per-source dedup is honoured later.
    parsed: list[tuple[float, list[tuple[str, str, str]]]] = []
    for text, weight in weighted_sources:
        urls: list[tuple[str, str, str]] = []
        for url in extract_urls(text):
            host = host_of(url)
            if not host:
                continue
            key = _registrable_key(host)
            if not key:
                continue
            urls.append((url, host, key))
        parsed.append((weight, urls))

    if not any(urls for _, urls in parsed):
        return None

    channel_token = _channel_name_token(channel_title) if channel_title else ""

    # Pass A: name-matched subdomain on a blocked-registrable host.
    if channel_token:
        for _, urls in parsed:
            for url, host, key in urls:
                if not _is_social_or_aggregator(key):
                    continue
                if host == key or not host.endswith("." + key):
                    continue
                prefix = host[: -len(key) - 1]
                prefix_norm = _channel_name_token(prefix)
                if prefix_norm and _longest_common_substring_len(channel_token, prefix_norm) >= 5:
                    return url

    # Pass B + C: weighted scoring across non-blocked registrables.
    score: Counter[str] = Counter()
    first_url: dict[str, str] = {}
    for weight, urls in parsed:
        seen_in_source: set[str] = set()
        for url, host, key in urls:
            if _is_social_or_aggregator(key):
                continue
            if key in seen_in_source:
                continue
            seen_in_source.add(key)
            score[key] += weight
            first_url.setdefault(key, f"https://{key}")

    if not score:
        return None

    if channel_token:
        matches = {k: s for k, s in score.items() if _name_overlap(channel_token, k)}
        if matches:
            return first_url[max(matches, key=matches.get)]

    return first_url[max(score, key=score.get)]


# YouTube wraps every outbound link as
# `https://www.youtube.com/redirect?...&q=<URL-encoded-destination>`.
# We match the `q=` parameter directly (works whether the wrapper is present
# in the HTML or just the inner JSON of `ytInitialData`).
_YT_REDIRECT_Q_RE = re.compile(r'q=(https?%3A%2F%2F[^&"\\\s]+)', re.IGNORECASE)


async def fetch_about_page_links(
    client: httpx.AsyncClient,
    channel_id: str,
) -> list[str]:
    """Scrape the channel's /about page for the URL targets behind YouTube's
    redirect wrappers. These are the channel's "Links" section + any inline
    description links — the highest-authority source for what the creator
    considers their own URLs. Returns deduped URLs; empty on any failure."""
    if not channel_id:
        return []
    url = f"https://www.youtube.com/channel/{channel_id}/about?hl=en"
    resp = await _get(client, url)
    if resp is None or resp.status_code != 200 or not resp.text:
        return []
    encoded = _YT_REDIRECT_Q_RE.findall(resp.text)
    out: list[str] = []
    seen: set[str] = set()
    for raw in encoded:
        try:
            u = unquote(raw)
        except Exception:
            continue
        if u and u not in seen and not u.startswith("https://www.youtube.com"):
            seen.add(u)
            out.append(u)
    return out


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
        # 1. Discover candidate URL. Weighted sources from highest authority
        # (channel's official Links section) down to least authoritative
        # (individual video descriptions, which often contain affiliate
        # promotions for third-party brands).
        about_links = await fetch_about_page_links(client, record.channel_id)
        weighted_sources: list[tuple[str, float]] = [
            ("\n".join(about_links), 5.0),
            (record.yt_channel_description, 2.0),
            *((d, 1.0) for d in record.yt_video_descriptions),
        ]
        candidate = pick_candidate_site(weighted_sources, channel_title=record.channel_title)
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

        # If the candidate redirected onto a blocked host (e.g. onelink.me →
        # appsflyer.com), treat as no site rather than letting downstream
        # stages enrich the wrapper service.
        if domain and _is_social_or_aggregator(domain):
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
