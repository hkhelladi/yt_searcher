"""Enrichment-pipeline constants and per-run config.

The defaults here can be overridden by the `enrichment:` block of a run_config
YAML. Anything not set in YAML falls back to these values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Static constants (rarely overridden per run) ────────────────────────────
ACTIVE_DAYS = 60                # M8 gate: channel considered active if latest upload within this
VIDEO_SAMPLE_SIZE = 10          # M2: recent videos sampled for duration + descriptions
SUBSCRIBER_MIN = 5_000          # M8 scoring band
SUBSCRIBER_MAX = 500_000
CRAWL_PATHS = ["/", "/about", "/shop", "/store", "/services", "/pricing", "/contact", "/work-with-me"]
HTTP_TIMEOUT_S = 10
TECH_DETECT_CONCURRENCY = 4     # Playwright is heavy; keep low
HTTP_CONCURRENCY = 16           # M3 crawl, M6/M7 lookups
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Default weights and tier thresholds for M8 — tune per offer.
DEFAULT_SCORE_WEIGHTS: dict[str, float] = {
    "has_affiliate_links": 3.0,
    "sells_or_ecom": 3.0,
    "in_subscriber_band": 2.0,
    "geo_north_america": 2.0,
    "social_two_plus": 1.0,
}
DEFAULT_TIER_THRESHOLDS: dict[str, float] = {"A": 7.0, "B": 4.0}  # ≥7 → A, ≥4 → B, else C

# Aggregator/social hosts dropped when picking a candidate `site_url` in M3.
SOCIAL_AND_AGGREGATOR_HOSTS = frozenset({
    "youtube.com", "youtu.be",
    "instagram.com", "tiktok.com",
    "twitter.com", "x.com",
    "facebook.com", "fb.com", "fb.me",
    "linkedin.com",
    "linktr.ee", "beacons.ai", "bio.link", "campsite.bio",
    "snapchat.com", "threads.net",
    "pinterest.com",
    "discord.gg", "discord.com",
    "t.me", "telegram.me",
    "patreon.com", "buymeacoffee.com", "ko-fi.com",
})


@dataclass
class EnrichmentConfig:
    """Per-run enrichment toggles, populated from the `enrichment:` YAML block."""

    enabled: bool = False
    tech_detect: bool = False              # M4 sub-flag
    hunter_api_key: str = ""               # M7 optional fallback
    limit: int | None = None               # M9 wave cap (top-N after sort)
    tier: str | None = None                # M9 wave tier filter ("A" / "B" / "C")
    exclude_shorts: bool = True            # M2: drop Shorts (<60s) from avg duration
    score_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SCORE_WEIGHTS))
    tier_thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TIER_THRESHOLDS))

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> EnrichmentConfig:
        if not raw:
            return cls()
        kwargs: dict[str, Any] = {}
        for key in ("enabled", "tech_detect", "hunter_api_key",
                    "limit", "tier", "exclude_shorts"):
            if key in raw:
                kwargs[key] = raw[key]
        if "score_weights" in raw and isinstance(raw["score_weights"], dict):
            kwargs["score_weights"] = {**DEFAULT_SCORE_WEIGHTS, **raw["score_weights"]}
        if "tier_thresholds" in raw and isinstance(raw["tier_thresholds"], dict):
            kwargs["tier_thresholds"] = {**DEFAULT_TIER_THRESHOLDS, **raw["tier_thresholds"]}
        return cls(**kwargs)
