"""M7 — Geo best-guess + contact email.

Geo priority: explicit yt_country > address/currency on /contact > TLD > UNKNOWN.
Contact email priority: site-scraped (/contact, /about) > Hunter.io fallback (opt-in).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from enrichment.config import HTTP_TIMEOUT_S, USER_AGENT
from enrichment.patterns import extract_emails
from enrichment.schema import EnrichedRecord


# ── Geo helpers ────────────────────────────────────────────────────────────

_TLD_TO_COUNTRY: dict[str, str] = {
    "ca": "CA",
    "us": "US",
}

# Province / state name fragments — case-insensitive substring match.
_CA_PROVINCES = (
    "alberta", "british columbia", "manitoba", "new brunswick", "newfoundland",
    "nova scotia", "ontario", "prince edward", "quebec", "québec",
    "saskatchewan", "yukon", "northwest territories", "nunavut",
)
_US_STATES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
)

_CA_POSTAL_RE = re.compile(r"\b[A-Z]\d[A-Z][ \-]?\d[A-Z]\d\b")
_US_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_CURRENCY_CAD_RE = re.compile(r"\bCAD\b|\bC\$|\bCA\$", re.IGNORECASE)
_CURRENCY_USD_RE = re.compile(r"\bUSD\b|\bUS\$", re.IGNORECASE)


def _tld_signal(domain: str | None) -> str | None:
    if not domain:
        return None
    tld = domain.rsplit(".", 1)[-1].lower() if "." in domain else ""
    return _TLD_TO_COUNTRY.get(tld)


def _address_signal(html: str) -> str | None:
    text_lc = (html or "").lower()
    has_ca_word = any(p in text_lc for p in _CA_PROVINCES)
    has_us_word = any(s in text_lc for s in _US_STATES)
    has_ca_postal = bool(_CA_POSTAL_RE.search(html or ""))
    has_us_zip = bool(_US_ZIP_RE.search(html or ""))

    if has_ca_postal or (has_ca_word and not has_us_word):
        return "CA"
    if has_us_zip or (has_us_word and not has_ca_word):
        return "US"
    return None


def _currency_signal(html: str) -> str | None:
    if _CURRENCY_CAD_RE.search(html or ""):
        return "CA"
    if _CURRENCY_USD_RE.search(html or ""):
        return "US"
    return None


def derive_geo(record: EnrichedRecord) -> tuple[str, dict[str, Any]]:
    """Apply the priority chain and return (geo_best_guess, geo_signals)."""
    signals: dict[str, Any] = {}

    # 1. Explicit channel country
    if record.yt_country:
        signals["yt_country"] = record.yt_country
        if record.yt_country in ("CA", "US"):
            return record.yt_country, signals
        return "OTHER", signals

    # 2. Address / currency on crawled HTML
    crawled = "\n".join(record.site_html_cache.values()) if record.site_html_cache else ""
    addr = _address_signal(crawled)
    curr = _currency_signal(crawled)
    if addr:
        signals["address"] = addr
    if curr:
        signals["currency"] = curr
    if addr in ("CA", "US"):
        return addr, signals
    if curr in ("CA", "US") and not addr:
        return curr, signals

    # 3. TLD
    tld_guess = _tld_signal(record.domain)
    if tld_guess:
        signals["tld"] = tld_guess
        return tld_guess, signals

    return "UNKNOWN", signals


# ── Contact email ──────────────────────────────────────────────────────────

# Skip the obvious junk that scrapes find on every WP page.
_EMAIL_BLOCKLIST_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "wordpress", "webmaster@example", "you@example", "name@example",
    "user@example", "info@example", "email@example",
)
_EMAIL_BLOCKLIST_DOMAINS = (
    "example.com", "example.org", "example.net", "sentry.io",
    "wixpress.com", "wordpress.com",
)


def _is_plausible_contact_email(email: str) -> bool:
    e = email.lower()
    if any(e.startswith(p) for p in _EMAIL_BLOCKLIST_PREFIXES):
        return False
    if any(e.endswith("@" + d) for d in _EMAIL_BLOCKLIST_DOMAINS):
        return False
    return True


def _pick_site_email(record: EnrichedRecord) -> str | None:
    # Prefer pages whose path looks contact-flavoured.
    contact_paths = [p for p in record.site_html_cache if "contact" in p or "about" in p]
    other_paths = [p for p in record.site_html_cache if p not in contact_paths]
    for path in contact_paths + other_paths:
        html = record.site_html_cache.get(path, "")
        for email in extract_emails(html):
            if _is_plausible_contact_email(email):
                return email
    return None


async def _hunter_fallback(domain: str, api_key: str) -> str | None:
    """Single Hunter.io domain-search call. Returns the highest-confidence
    email or None. Skip silently on any error."""
    if not domain or not api_key:
        return None
    url = "https://api.hunter.io/v2/domain-search"
    params = {"domain": domain, "api_key": api_key, "limit": 5}
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT_S,
        ) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return None
            data = resp.json()
    except (httpx.HTTPError, ValueError, OSError):
        return None

    emails = (data.get("data") or {}).get("emails") or []
    if not emails:
        return None
    # Prefer "verified" status, then any plausible address.
    emails.sort(key=lambda e: 0 if e.get("verification", {}).get("status") == "valid" else 1)
    for e in emails:
        addr = e.get("value")
        if addr and _is_plausible_contact_email(addr):
            return addr
    return None


async def _enrich_one(record: EnrichedRecord, hunter_key: str) -> EnrichedRecord:
    # Geo
    guess, signals = derive_geo(record)
    record.geo_best_guess = guess
    record.geo_signals = signals

    # Site email
    email = _pick_site_email(record)
    if email:
        record.contact_email = email
        record.contact_source = "site"
    elif hunter_key and record.domain:
        fallback = await _hunter_fallback(record.domain, hunter_key)
        if fallback:
            record.contact_email = fallback
            record.contact_source = "hunter"

    if "M7" not in record.stages_completed:
        record.stages_completed.append("M7")
    return record


async def stage_geo_contact(
    records: list[EnrichedRecord],
    hunter_api_key: str = "",
) -> list[EnrichedRecord]:
    if not records:
        return records
    return await asyncio.gather(*(_enrich_one(r, hunter_api_key) for r in records))
