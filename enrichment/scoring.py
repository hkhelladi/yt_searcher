"""M8 — Gates + weighted scoring + tier mapping. Pure function.

Gates flag rows but do not drop them (the export stage keeps gated rows in the
CSV with the `gate_failures` column populated, so the user can audit them).
The CASL compliance flag also lives here so it travels with the scored record.
"""

from __future__ import annotations

from datetime import date, timedelta

from enrichment.config import (
    ACTIVE_DAYS,
    EnrichmentConfig,
    SUBSCRIBER_MAX,
    SUBSCRIBER_MIN,
)
from enrichment.schema import EnrichedRecord


def _gates(record: EnrichedRecord, today: date) -> list[str]:
    failures: list[str] = []
    cutoff = today - timedelta(days=ACTIVE_DAYS)
    if not record.yt_latest_video_at or record.yt_latest_video_at < cutoff:
        failures.append("inactive")
    if not record.site_resolved:
        failures.append("no_site")
    if not record.contact_email:
        failures.append("no_email")
    return failures


def _score(record: EnrichedRecord, weights: dict[str, float]) -> float:
    score = 0.0
    if record.has_affiliate_links:
        score += weights.get("has_affiliate_links", 0)
    if record.site_is_ecommerce or record.site_sells_services:
        score += weights.get("sells_or_ecom", 0)
    subs = record.yt_subscribers
    if subs is not None and SUBSCRIBER_MIN <= subs <= SUBSCRIBER_MAX:
        score += weights.get("in_subscriber_band", 0)
    if record.geo_best_guess in ("CA", "US"):
        score += weights.get("geo_north_america", 0)
    if len(record.social_profiles) >= 2:
        score += weights.get("social_two_plus", 0)
    return round(score, 2)


def _tier(score: float, thresholds: dict[str, float]) -> str:
    a = thresholds.get("A", 7.0)
    b = thresholds.get("B", 4.0)
    if score >= a:
        return "A"
    if score >= b:
        return "B"
    return "C"


def stage_score(
    records: list[EnrichedRecord],
    config: EnrichmentConfig,
    today: date | None = None,
) -> list[EnrichedRecord]:
    today = today or date.today()
    for r in records:
        r.gate_failures = _gates(r, today)
        r.score = _score(r, config.score_weights)
        r.tier = _tier(r.score, config.tier_thresholds)
        r.compliance_flag = "CASL_REVIEW" if r.geo_best_guess == "CA" else "OK"
        if "M8" not in r.stages_completed:
            r.stages_completed.append("M8")
    return records
