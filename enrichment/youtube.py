"""M2 — YouTube Data API v3 enrichment.

Seeds an EnrichedRecord from a raw `channels.list` resource (already fetched by
the search stage with `part=snippet,statistics,contentDetails,topicDetails,
brandingSettings`), then adds:

  - the uploads-playlist sample (latest videos + descriptions)
  - the per-video duration + categoryId distribution
  - the derived `content_type` label

All YouTube calls go via the existing sync `googleapiclient` build — no async
here; quota is the bottleneck, not latency.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime
from typing import Any

import isodate
from googleapiclient.errors import HttpError

from enrichment.schema import EnrichedRecord


# Coarse mapping from YouTube category IDs to a content_type bucket.
# Reference: https://developers.google.com/youtube/v3/docs/videoCategories/list
_CATEGORY_TO_TYPE: dict[str, str] = {
    "1":  "film",          # Film & Animation
    "2":  "auto",          # Autos & Vehicles
    "10": "music",
    "15": "pets",
    "17": "sports",
    "19": "travel",
    "20": "gaming",
    "22": "vlog",          # People & Blogs
    "23": "comedy",
    "24": "entertainment",
    "25": "news",
    "26": "howto",         # Howto & Style
    "27": "education",
    "28": "tech",          # Science & Technology
    "29": "nonprofit",
}


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def seed_from_resource(resource: dict) -> EnrichedRecord:
    """Build an EnrichedRecord from a raw channels.list resource."""

    snippet = resource.get("snippet", {}) or {}
    stats = resource.get("statistics", {}) or {}
    topics = resource.get("topicDetails", {}) or {}
    branding = (resource.get("brandingSettings", {}) or {}).get("channel", {}) or {}

    hidden = bool(stats.get("hiddenSubscriberCount"))
    subs = None if hidden else _safe_int(stats.get("subscriberCount"))

    record = EnrichedRecord(
        channel_id=resource.get("id", ""),
        channel_title=snippet.get("title", "") or branding.get("title", ""),
        yt_subscribers=subs,
        yt_subscribers_hidden=hidden,
        yt_total_views=_safe_int(stats.get("viewCount")) or 0,
        yt_video_count=_safe_int(stats.get("videoCount")) or 0,
        yt_created_at=_parse_iso_date(snippet.get("publishedAt")),
        yt_country=snippet.get("country") or branding.get("country"),
        yt_topic_categories=list(topics.get("topicCategories", []) or []),
        yt_channel_description=snippet.get("description", "") or "",
    )
    return record


def _uploads_playlist_id(resource: dict) -> str:
    return (
        (resource.get("contentDetails", {}) or {})
        .get("relatedPlaylists", {})
        .get("uploads", "")
    )


def fetch_video_sample(
    youtube,
    channel_resources: dict[str, dict],
    sample_size: int,
) -> dict[str, list[dict]]:
    """For each channel, return the latest `sample_size` videos as dicts with
    `videoId`, `publishedAt`, `description`. Empty list on any failure.
    """
    out: dict[str, list[dict]] = {}
    for cid, resource in channel_resources.items():
        playlist_id = _uploads_playlist_id(resource)
        if not playlist_id:
            out[cid] = []
            continue
        try:
            resp = youtube.playlistItems().list(
                playlistId=playlist_id,
                part="contentDetails,snippet",
                maxResults=min(50, max(sample_size, 1)),
            ).execute()
        except HttpError:
            out[cid] = []
            continue

        items = []
        for it in resp.get("items", []):
            cd = it.get("contentDetails", {}) or {}
            sn = it.get("snippet", {}) or {}
            vid = cd.get("videoId")
            if not vid:
                continue
            items.append({
                "videoId": vid,
                "publishedAt": cd.get("videoPublishedAt") or sn.get("publishedAt"),
                "description": sn.get("description", "") or "",
            })
        out[cid] = items[:sample_size]
    return out


def fetch_video_details(youtube, video_ids: list[str]) -> dict[str, dict]:
    """Batch videos.list (50 IDs/call) and return {videoId: {duration_s, categoryId, description}}."""
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        try:
            resp = youtube.videos().list(
                part="contentDetails,snippet",
                id=",".join(batch),
                maxResults=50,
            ).execute()
        except HttpError:
            continue
        for item in resp.get("items", []):
            vid = item.get("id")
            if not vid:
                continue
            cd = item.get("contentDetails", {}) or {}
            sn = item.get("snippet", {}) or {}
            try:
                seconds = int(isodate.parse_duration(cd.get("duration", "PT0S")).total_seconds())
            except Exception:
                seconds = 0
            out[vid] = {
                "duration_s": seconds,
                "categoryId": sn.get("categoryId"),
                "description": sn.get("description", "") or "",
            }
    return out


def _derive_content_type(category_id: str | None, topic_categories: list[str]) -> str:
    if category_id and category_id in _CATEGORY_TO_TYPE:
        return _CATEGORY_TO_TYPE[category_id]
    blob = " ".join(topic_categories).lower()
    if "lifestyle" in blob or "society" in blob:
        return "vlog"
    if "technology" in blob:
        return "tech"
    if "entertainment" in blob:
        return "entertainment"
    if "knowledge" in blob:
        return "education"
    if "sport" in blob:
        return "sports"
    if "music" in blob:
        return "music"
    return "other"


def populate_video_signals(
    record: EnrichedRecord,
    sample: list[dict],
    video_details: dict[str, dict],
    exclude_shorts: bool,
) -> EnrichedRecord:
    """Mutate `record` with avg/median seconds, latest upload date, category, content_type."""
    if not sample:
        return record

    # Latest upload from the sample
    dates = [_parse_iso_date(v["publishedAt"]) for v in sample if v.get("publishedAt")]
    dates = [d for d in dates if d is not None]
    if dates:
        record.yt_latest_video_at = max(dates)

    # Video descriptions for downstream M3/M5
    descriptions = [v.get("description", "") for v in sample]
    # Merge in the longer descriptions from videos.list when available
    for v in sample:
        vid = v.get("videoId")
        if vid and vid in video_details:
            full = video_details[vid].get("description") or ""
            if len(full) > len(v.get("description", "")):
                idx = sample.index(v)
                if idx < len(descriptions):
                    descriptions[idx] = full
    record.yt_video_descriptions = descriptions

    # Durations + categoryId
    durations: list[int] = []
    categories: list[str] = []
    for v in sample:
        vid = v.get("videoId")
        det = video_details.get(vid) if vid else None
        if not det:
            continue
        secs = det.get("duration_s") or 0
        if exclude_shorts and secs and secs < 60:
            pass
        elif secs:
            durations.append(secs)
        cat = det.get("categoryId")
        if cat:
            categories.append(cat)

    if durations:
        record.yt_avg_video_seconds = int(sum(durations) / len(durations))
        record.yt_median_video_seconds = int(statistics.median(durations))

    if categories:
        # Most common categoryId
        record.yt_content_category_id = max(set(categories), key=categories.count)

    record.content_type = _derive_content_type(
        record.yt_content_category_id, record.yt_topic_categories
    )
    return record


def stage_youtube(
    youtube,
    records: list[EnrichedRecord],
    channel_resources: dict[str, dict],
    sample_size: int,
    exclude_shorts: bool,
) -> list[EnrichedRecord]:
    """M2 stage: enrich every record with video-sample-derived signals."""
    if not records:
        return records

    by_id = {r.channel_id: r for r in records}

    # Pull latest uploads per channel
    samples = fetch_video_sample(
        youtube,
        {cid: channel_resources[cid] for cid in by_id if cid in channel_resources},
        sample_size,
    )

    # Pull video details in batches
    all_video_ids = [v["videoId"] for s in samples.values() for v in s]
    details = fetch_video_details(youtube, all_video_ids)

    for cid, rec in by_id.items():
        populate_video_signals(rec, samples.get(cid, []), details, exclude_shorts)
        if "M2" not in rec.stages_completed:
            rec.stages_completed.append("M2")

    return list(by_id.values())
