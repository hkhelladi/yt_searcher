"""M5 — Shared regex / pattern extraction.

One module applied to two inputs:
  (a) YouTube descriptions (channel + sampled videos) — M3 also uses the URL
      regex here to discover the candidate site,
  (b) site HTML from M3.

The affiliate-network table is data-driven; extend it by editing the table,
not the matching logic.
"""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urlparse

from enrichment.schema import EnrichedRecord


# ── Regexes ─────────────────────────────────────────────────────────────────

URL_RE = re.compile(
    r"https?://(?:www\.)?[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z]{2,24}"
    r"(?:[-a-zA-Z0-9()@:%_+.~#?&/=]*)",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
MAILTO_RE = re.compile(r"mailto:([^\s\"'>]+)", re.IGNORECASE)


# ── Affiliate network table (host pattern → network label) ──────────────────
# Patterns are matched as substrings against the URL host (and the full URL
# for cases where the network is encoded in a path/param).
AFFILIATE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"amzn\.to|amazon\.[^/]+/.*[?&]tag=", re.I),     "Amazon Associates"),
    (re.compile(r"\.pxf\.io|\.sjv\.io|\.ojrq\.net", re.I),       "Impact"),
    (re.compile(r"anrdoezrs\.net|dpbolvw\.net|tkqlhce\.com|jdoqocy\.com", re.I), "CJ Affiliate"),
    (re.compile(r"click\.linksynergy\.com", re.I),               "Rakuten"),
    (re.compile(r"shrsl\.com|shareasale\.com", re.I),            "ShareASale"),
    (re.compile(r"geni\.us", re.I),                              "Geniuslink"),
    (re.compile(r"gumroad\.com", re.I),                          "Gumroad"),
    (re.compile(r"avantlink\.com", re.I),                        "AvantLink"),
    (re.compile(r"awin1?\.com|awin\.com", re.I),                 "Awin"),
    (re.compile(r"clickbank\.net", re.I),                        "ClickBank"),
    (re.compile(r"partnerize\.com|prf\.hn", re.I),               "Partnerize"),
    (re.compile(r"refersion\.com", re.I),                        "Refersion"),
]


# ── Services-detection phrases ─────────────────────────────────────────────
SERVICES_PHRASES: list[re.Pattern] = [
    re.compile(r"work with me", re.I),
    re.compile(r"\bhire me\b", re.I),
    re.compile(r"book a (?:call|consultation|session)", re.I),
    re.compile(r"\b1[\s:-]?on[\s:-]?1\b|\b1:1\b", re.I),
    re.compile(r"book now", re.I),
    re.compile(r"schedule a call", re.I),
    re.compile(r"free consultation", re.I),
]
SERVICES_PATHS = ("/services", "/pricing", "/book", "/consulting", "/coaching", "/work-with-me", "/hire-me")
SERVICES_EMBED_HOSTS = ("calendly.com", "cal.com", "savvycal.com", "tidycal.com")


# ── Social-profile extraction ──────────────────────────────────────────────
SOCIAL_PATTERNS: dict[str, re.Pattern] = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)", re.I),
    "tiktok":    re.compile(r"https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]+)", re.I),
    "twitter":   re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]+)", re.I),
    "linkedin":  re.compile(r"https?://(?:www\.)?linkedin\.com/(?:in|company)/([A-Za-z0-9\-_.%]+)", re.I),
    "facebook":  re.compile(r"https?://(?:www\.)?facebook\.com/([A-Za-z0-9.\-]+)", re.I),
    "youtube":   re.compile(r"https?://(?:www\.)?youtube\.com/(?:@|c/|channel/|user/)([A-Za-z0-9_\-.%]+)", re.I),
}

# Common handle-tracking suffixes / generic paths to skip.
_SOCIAL_HANDLE_SKIP = frozenset({"sharer", "share", "intent", "home", "pages", "people", "tr", "watch"})


# ── Helpers ─────────────────────────────────────────────────────────────────

def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    return URL_RE.findall(text)


def extract_emails(text: str) -> list[str]:
    if not text:
        return []
    found = list(MAILTO_RE.findall(text)) + list(EMAIL_RE.findall(text))
    # Dedup preserving order
    seen = set()
    out = []
    for e in found:
        e = e.strip().rstrip(".,;:)>\"'")
        if e and e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


def host_of(url: str) -> str:
    try:
        h = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return h.lower().lstrip(".").removeprefix("www.")


def detect_affiliates(urls: Iterable[str]) -> list[str]:
    found: list[str] = []
    for u in urls:
        for pat, label in AFFILIATE_PATTERNS:
            if pat.search(u):
                if label not in found:
                    found.append(label)
                break
    return found


def detect_services(text: str, urls: Iterable[str]) -> bool:
    text_lc = text or ""
    for pat in SERVICES_PHRASES:
        if pat.search(text_lc):
            return True
    for u in urls:
        host = host_of(u)
        if any(host == h or host.endswith("." + h) for h in SERVICES_EMBED_HOSTS):
            return True
        path = (urlparse(u).path or "").lower()
        if any(path.startswith(p) for p in SERVICES_PATHS):
            return True
    return False


def extract_social_profiles(text: str) -> dict[str, str]:
    profiles: dict[str, str] = {}
    if not text:
        return profiles
    for platform, pat in SOCIAL_PATTERNS.items():
        for match in pat.finditer(text):
            handle = match.group(1).strip("/").rstrip(".,;:)>\"'")
            if not handle or handle.lower() in _SOCIAL_HANDLE_SKIP:
                continue
            # Build canonical URL
            canonical = {
                "instagram": f"https://instagram.com/{handle}",
                "tiktok":    f"https://tiktok.com/@{handle}",
                "twitter":   f"https://x.com/{handle}",
                "linkedin":  f"https://linkedin.com/in/{handle}",
                "facebook":  f"https://facebook.com/{handle}",
                "youtube":   f"https://youtube.com/@{handle}",
            }[platform]
            profiles.setdefault(platform, canonical)
            break
    return profiles


def stage_patterns(record: EnrichedRecord) -> EnrichedRecord:
    """M5 — combine signals from YT descriptions + crawled HTML."""

    # Combine all text we have for this channel
    yt_text = "\n".join([record.yt_channel_description, *record.yt_video_descriptions])
    html_text = "\n".join(record.site_html_cache.values()) if record.site_html_cache else ""
    combined = yt_text + "\n" + html_text

    urls = extract_urls(combined)

    # Affiliate
    networks = detect_affiliates(urls)
    record.affiliate_networks = networks
    record.has_affiliate_links = bool(networks)

    # Services
    # If tech detection has already flagged ecommerce we don't override,
    # but services can coexist with ecommerce.
    record.site_sells_services = detect_services(combined, urls)

    # Social profiles — prefer YT descriptions over crawled HTML (less noisy)
    profiles = extract_social_profiles(yt_text)
    for k, v in extract_social_profiles(html_text).items():
        profiles.setdefault(k, v)
    # Don't store the channel's own YouTube link as a "social profile"
    profiles.pop("youtube", None)
    record.social_profiles = profiles

    if "M5" not in record.stages_completed:
        record.stages_completed.append("M5")
    return record
