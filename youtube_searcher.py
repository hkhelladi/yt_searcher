"""
YouTube Channel Searcher
────────────────────────
Searches YouTube for channels matching configurable criteria and writes results
to a CSV file that includes both search config metadata and channel details.

Usage:
    python youtube_searcher.py                                    # run all searches in config.yaml
    python youtube_searcher.py --config examples/ca_mortgage_brokers.yaml
    python youtube_searcher.py --search ca_mortgage_brokers_en   # run one search by name
    python youtube_searcher.py --dry-run                         # quota estimate, no API calls

Output is always written to outputs/<search_name>.csv  (single search)
                          or outputs/<config_output_csv> (all searches)

Quota cost per run (YouTube Data API v3, free tier = 10,000 units/day):
    search.list  : 100 units per call  (1 call per 50 results requested)
    channels.list:   1 unit per call   (1 call per 50 channels enriched)
    Estimate is printed before each search block.
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from math import ceil

import yaml
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from enrichment import EnrichmentConfig, run_enrichment
from enrichment.orchestrator import apply_wave_filter
from enrichment.schema import EnrichedRecord

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


# ─── Regex patterns for contact extraction ───────────────────────────────────
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)
_URL_RE = re.compile(
    r"https?://(?:www\.)?[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z]{2,6}"
    r"(?:[-a-zA-Z0-9()@:%_+.~#?&/=]*)"
)
_PHONE_RE = re.compile(
    r"(?:\+?1[\s\-.]?)?"                         # optional country code
    r"\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}"    # (XXX) XXX-XXXX variants
)

# CSV output columns (in order). The enrichment block at the end is only
# written when the run_config has `enrichment.enabled: true`.
CSV_FIELDS = [
    # ── Search config (so each row is self-documenting) ──
    "search_name",
    "keywords",
    "region_code",
    "language",
    "order",
    "search_type",
    # ── Channel identity ──
    "channel_name",
    "channel_id",
    "custom_url",
    "channel_url",
    "country",
    "published_at",
    # ── Channel stats ──
    "subscribers",
    "total_views",
    "video_count",
    # ── Channel activity / liveness ──
    "last_upload_at",
    "days_since_last_upload",
    "uploads_last_6mo",
    # ── Contact / business info ──
    "contact_email",
    "websites",
    "phone_numbers",
    "description_snippet",
    # ── Meta ──
    "fetched_at",
]

# Appended to CSV_FIELDS when enrichment is enabled.
ENRICHMENT_FIELDS = [
    "yt_topic_categories",
    "yt_avg_video_seconds",
    "yt_median_video_seconds",
    "content_type",
    "site_url",
    "site_resolved",
    "site_final_url",
    "domain",
    "domain_created_at",
    "site_type",
    "site_is_dynamic",
    "site_is_ecommerce",
    "site_technologies",
    "has_affiliate_links",
    "affiliate_networks",
    "site_sells_services",
    "social_profiles",
    "traffic_rank",
    "geo_best_guess",
    "contact_source",
    "score",
    "tier",
    "gate_failures",
    "compliance_flag",
    "enriched_at",
]

# When written to CSV these are pulled to the front of every row in this
# exact order. Any of them that doesn't exist in the current field set
# (e.g. enrichment-only columns when enrichment is off) is silently skipped.
PRIORITY_FIELDS = [
    "channel_name", "channel_url", "country",
    "subscribers", "total_views", "video_count",
    "days_since_last_upload",
    "site_final_url", "description_snippet",
    "site_is_dynamic", "site_is_ecommerce", "site_technologies",
    "has_affiliate_links", "site_sells_services",
    "contact_email", "phone_numbers", "social_profiles",
]


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # Resolution order: env var > config file value
    api_key = os.getenv("YOUTUBE_API_KEY") or cfg.get("api_key", "")
    if not api_key or api_key == "YOUR_YOUTUBE_DATA_API_V3_KEY":
        sys.exit(
            "ERROR: YouTube Data API v3 key not found.\n"
            "Set it in a .env file as YOUTUBE_API_KEY=your_key\n"
            "or export it in your shell: export YOUTUBE_API_KEY=your_key\n"
            "Get a key at https://console.cloud.google.com/ → APIs & Services → Credentials."
        )
    cfg["api_key"] = api_key
    return cfg


def estimate_quota(max_results: int) -> dict:
    pages = ceil(max_results / 50)
    channels_calls = ceil(max_results / 50)
    search_cost = pages * 100
    channels_cost = channels_calls * 1
    return {
        "search_calls": pages,
        "channels_calls": channels_calls,
        "search_units": search_cost,
        "channels_units": channels_cost,
        "total_units": search_cost + channels_cost,
    }


def search_channels(youtube, search_cfg: dict) -> list[dict]:
    """
    Calls search.list to collect channel IDs, then channels.list to enrich them.
    Returns a list of raw channel resource dicts from the API.
    """
    keywords     = search_cfg["keywords"]
    region_code  = search_cfg.get("region_code", "")
    language     = search_cfg.get("language", "")
    max_results  = int(search_cfg.get("max_results", 50))
    order        = search_cfg.get("order", "relevance")
    search_type  = search_cfg.get("search_type", "channel")

    channel_ids: list[str] = []
    page_token: str | None = None
    remaining = max_results

    while remaining > 0:
        page_size = min(50, remaining)
        params: dict = dict(
            part="snippet",
            q=keywords,
            type=search_type,
            maxResults=page_size,
            order=order,
        )
        if region_code:
            params["regionCode"] = region_code
        if language:
            params["relevanceLanguage"] = language
        if page_token:
            params["pageToken"] = page_token

        resp = youtube.search().list(**params).execute()

        for item in resp.get("items", []):
            cid = (
                item.get("id", {}).get("channelId")
                if search_type == "channel"
                else item.get("snippet", {}).get("channelId")
            )
            if cid and cid not in channel_ids:
                channel_ids.append(cid)

        page_token = resp.get("nextPageToken")
        remaining -= page_size
        if not page_token:
            break

    return enrich_channels(youtube, channel_ids)


def enrich_channels(youtube, channel_ids: list[str]) -> list[dict]:
    """Calls channels.list in batches of 50 to get full channel details.

    Includes `topicDetails` so the enrichment pipeline (M2) has topic categories
    available without an extra API call. Quota cost stays at 1 unit per batch.
    """
    results = []
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i : i + 50]
        resp = youtube.channels().list(
            part="snippet,statistics,brandingSettings,contentDetails,topicDetails",
            id=",".join(batch),
            maxResults=50,
        ).execute()
        results.extend(resp.get("items", []))
    return results


def get_channel_activity(youtube, uploads_playlist_id: str, now_utc: datetime | None = None) -> dict:
    """Return liveness signals for a channel by reading its uploads playlist.

    Fetches the latest 50 uploads (1 quota unit) and derives:
      - last_upload_at         : YYYY-MM-DD of the most recent video
      - days_since_last_upload : int days from now to that upload
      - uploads_last_6mo       : count of uploads in the past 180 days (capped at 50)

    All fields are empty strings on any failure (no playlist, deleted, API error).
    """
    blank = {"last_upload_at": "", "days_since_last_upload": "", "uploads_last_6mo": ""}
    if not uploads_playlist_id:
        return blank
    try:
        resp = youtube.playlistItems().list(
            playlistId=uploads_playlist_id,
            part="contentDetails",
            maxResults=50,
        ).execute()
    except HttpError:
        return blank

    items = resp.get("items", [])
    if not items:
        return blank

    now = now_utc or datetime.now(timezone.utc)
    six_months_ago = now - timedelta(days=180)

    timestamps: list[datetime] = []
    for item in items:
        ts_str = item.get("contentDetails", {}).get("videoPublishedAt")
        if not ts_str:
            continue
        try:
            timestamps.append(datetime.fromisoformat(ts_str.replace("Z", "+00:00")))
        except ValueError:
            continue

    if not timestamps:
        return blank

    timestamps.sort(reverse=True)
    last = timestamps[0]
    return {
        "last_upload_at":         last.strftime("%Y-%m-%d"),
        "days_since_last_upload": (now - last).days,
        "uploads_last_6mo":       sum(1 for t in timestamps if t >= six_months_ago),
    }


def extract_contact_info(text: str) -> tuple[str, str, str]:
    """Extract emails, website URLs, and phone numbers from free text."""
    if not text:
        return "", "", ""
    emails   = list(dict.fromkeys(_EMAIL_RE.findall(text)))
    websites = list(dict.fromkeys(
        u for u in _URL_RE.findall(text)
        if "youtube.com" not in u and "youtu.be" not in u
    ))
    phones   = list(dict.fromkeys(_PHONE_RE.findall(text)))
    return (
        " | ".join(emails),
        " | ".join(websites),
        " | ".join(phones),
    )


def channel_to_row(channel: dict, search_cfg: dict, fetched_at: str) -> dict:
    snippet   = channel.get("snippet", {})
    stats     = channel.get("statistics", {})
    branding  = channel.get("brandingSettings", {}).get("channel", {})
    content   = channel.get("contentDetails", {})
    uploads_playlist_id = content.get("relatedPlaylists", {}).get("uploads", "")

    description = snippet.get("description", "")
    emails, websites, phones = extract_contact_info(description)

    # brandingSettings sometimes has an explicit email field
    branding_email = branding.get("email", "")
    if branding_email and branding_email not in emails:
        emails = (emails + " | " + branding_email).strip(" | ")

    channel_id  = channel.get("id", "")
    custom_url  = snippet.get("customUrl", "")
    channel_url = (
        f"https://www.youtube.com/{custom_url}"
        if custom_url
        else f"https://www.youtube.com/channel/{channel_id}"
    )

    subscribers = stats.get("subscriberCount", "hidden")
    if subscribers != "hidden":
        subscribers = int(subscribers)

    return {
        "search_name":        search_cfg.get("name", ""),
        "keywords":           search_cfg.get("keywords", ""),
        "region_code":        search_cfg.get("region_code", ""),
        "language":           search_cfg.get("language", ""),
        "order":              search_cfg.get("order", ""),
        "search_type":        search_cfg.get("search_type", ""),
        "channel_name":       snippet.get("title", ""),
        "channel_id":         channel_id,
        "custom_url":         custom_url,
        "channel_url":        channel_url,
        "country":            snippet.get("country", ""),
        "published_at":       snippet.get("publishedAt", ""),
        "subscribers":        subscribers,
        "total_views":        int(stats.get("viewCount", 0)),
        "video_count":        int(stats.get("videoCount", 0)),
        # Activity fields populated post-dedup in run_searches
        "last_upload_at":         "",
        "days_since_last_upload": "",
        "uploads_last_6mo":       "",
        "_uploads_playlist_id":   uploads_playlist_id,  # internal, popped before CSV write
        # Emails extracted from the YouTube channel description + brandingSettings.
        # When enrichment runs, the M7 site-scrape / Hunter result is merged into
        # this same column via _merge_emails().
        "contact_email":      emails,
        "websites":           websites,
        "phone_numbers":      phones,
        "description_snippet": description[:300].replace("\n", " "),
        "fetched_at":         fetched_at,
    }


def apply_filters(rows: list[dict], search_cfg: dict) -> list[dict]:
    min_sub = search_cfg.get("min_subscribers")
    max_sub = search_cfg.get("max_subscribers")
    filtered = []
    for row in rows:
        subs = row["subscribers"]
        if subs == "hidden":
            filtered.append(row)
            continue
        if min_sub is not None and subs < int(min_sub):
            continue
        if max_sub is not None and subs > int(max_sub):
            continue
        filtered.append(row)
    return filtered


def _fmt(value) -> str:
    """Best-effort CSV stringification for enrichment field values."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (list, tuple)):
        return " | ".join(str(v) for v in value)
    if isinstance(value, dict):
        return " | ".join(f"{k}:{v}" for k, v in value.items())
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _merge_emails(*sources: str) -> str:
    """Union and dedupe (case-insensitive) ' | '-separated email lists from
    multiple sources. Used to fold the M7 site-scraped / Hunter email back
    into the same column as the YouTube-description-scraped emails."""
    seen: set[str] = set()
    out: list[str] = []
    for src in sources:
        if not src:
            continue
        for raw in str(src).split(" | "):
            e = raw.strip()
            if not e:
                continue
            lo = e.lower()
            if lo in seen:
                continue
            seen.add(lo)
            out.append(e)
    return " | ".join(out)


def enrichment_row(record: EnrichedRecord | None) -> dict:
    """Convert an EnrichedRecord into the CSV columns named in ENRICHMENT_FIELDS."""
    if record is None:
        return {k: "" for k in ENRICHMENT_FIELDS}
    return {
        "yt_topic_categories":   _fmt(record.yt_topic_categories),
        "yt_avg_video_seconds":  _fmt(record.yt_avg_video_seconds),
        "yt_median_video_seconds": _fmt(record.yt_median_video_seconds),
        "content_type":          _fmt(record.content_type),
        "site_url":              _fmt(record.site_url),
        "site_resolved":         _fmt(record.site_resolved),
        "site_final_url":        _fmt(record.site_final_url),
        "domain":                _fmt(record.domain),
        "domain_created_at":     _fmt(record.domain_created_at),
        "site_type":             _fmt(record.site_type),
        "site_is_dynamic":       _fmt(record.site_is_dynamic),
        "site_is_ecommerce":     _fmt(record.site_is_ecommerce),
        "site_technologies":     _fmt(record.site_technologies),
        "has_affiliate_links":   _fmt(record.has_affiliate_links),
        "affiliate_networks":    _fmt(record.affiliate_networks),
        "site_sells_services":   _fmt(record.site_sells_services),
        "social_profiles":       _fmt(record.social_profiles),
        "traffic_rank":          _fmt(record.traffic_rank),
        "geo_best_guess":        _fmt(record.geo_best_guess),
        "contact_source":        _fmt(record.contact_source),
        "score":                 _fmt(record.score),
        "tier":                  _fmt(record.tier),
        "gate_failures":         _fmt(record.gate_failures),
        "compliance_flag":       _fmt(record.compliance_flag),
        "enriched_at":           _fmt(record.enriched_at),
    }


def order_csv_fields(base: list[str], priority: list[str]) -> list[str]:
    """Return `base` reordered so that columns named in `priority` appear
    first in priority order (skipping any not in base), followed by the
    remaining base columns in their original relative order."""
    base_set = set(base)
    leading = [f for f in priority if f in base_set]
    leading_set = set(leading)
    trailing = [f for f in base if f not in leading_set]
    return leading + trailing


OUTPUTS_DIR = "outputs"



def run_searches(
    searches: list[dict],
    output_name: str | None = None,
    dry_run: bool = False,
    output_path: str | None = None,
    show_quota: bool = True,
    enrichment_config: EnrichmentConfig | None = None,
):
    """Run a list of search config dicts and write results to a single CSV.

    This is the core execution function — it takes ready-to-go search dicts
    rather than loading from a config file. Used by both run() and batch mode.

    Either `output_name` (CSV goes to outputs/{output_name}_{ts}.csv) or
    `output_path` (explicit file path) must be provided.

    When `enrichment_config.enabled` is True, the deduplicated rows are passed
    through the enrichment pipeline (`enrichment.run_enrichment`) and the CSV
    is extended with the columns named in ENRICHMENT_FIELDS. The SQLite cache
    is persisted under `outputs/<output_name>/enrichment.db` so subsequent
    runs of the same config skip stages that have already completed.
    """
    api_key = os.getenv("YOUTUBE_API_KEY", "")
    if not api_key:
        sys.exit(
            "ERROR: YouTube Data API v3 key not found.\n"
            "Set it in a .env file as YOUTUBE_API_KEY=your_key\n"
            "or export it in your shell: export YOUTUBE_API_KEY=your_key"
        )

    if output_path:
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        output = output_path
    else:
        if not output_name:
            sys.exit("ERROR: run_searches requires either output_name or output_path.")
        os.makedirs(OUTPUTS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = os.path.join(OUTPUTS_DIR, f"{output_name}_{ts}.csv")

    # ── Quota estimate ───────────────────────────────────────
    if show_quota:
        print("\n━━ Quota cost estimate ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        total_units = 0
        for s in searches:
            q = estimate_quota(s.get("max_results", 50))
            total_units += q["total_units"]
            print(
                f"  [{s.get('name','')}]  {s.get('max_results', 50)} results → "
                f"{q['search_calls']} search call(s) ({q['search_units']} units) + "
                f"{q['channels_calls']} channels call(s) ({q['channels_units']} units) "
                f"= {q['total_units']} units"
            )
        max_activity = sum(int(s.get("max_results", 50)) for s in searches)
        print(f"\n  TOTAL THIS RUN : {total_units} units (search + channels)")
        print(f"  + activity fetch: 1 unit per unique channel (≤ {max_activity} extra, less after dedup)")
        print( "  Free daily quota: 10,000 units")
        print(f"  Remaining after : ~{10_000 - total_units - max_activity}–{10_000 - total_units} units")
        if total_units > 10_000:
            print("  ⚠  Exceeds free daily quota — enable billing or reduce max_results.")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

    if dry_run:
        print("Dry-run mode: no API calls made.")
        return

    # ── Execute searches ─────────────────────────────────────
    youtube    = build("youtube", "v3", developerKey=api_key)
    all_rows   = []
    channel_resources: dict[str, dict] = {}   # channel_id → raw channels.list resource
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for s in searches:
        print(f"Searching: [{s.get('name','')}]  keywords='{s.get('keywords','')}'  "
              f"region={s.get('region_code','')}  max={s.get('max_results',50)} ...")
        try:
            channels = search_channels(youtube, s)
        except HttpError as e:
            err = json.loads(e.content)
            print(f"  API error: {err['error']['message']}")
            continue

        for ch in channels:
            cid = ch.get("id", "")
            if cid and cid not in channel_resources:
                channel_resources[cid] = ch

        rows = [channel_to_row(ch, s, fetched_at) for ch in channels]
        rows = apply_filters(rows, s)
        print(f"  → {len(rows)} channel(s) after filters")
        all_rows.extend(rows)

    # ── Deduplicate by channel_id (keep first occurrence) ────
    seen = set()
    unique_rows = []
    for row in all_rows:
        cid = row["channel_id"]
        if cid not in seen:
            seen.add(cid)
            unique_rows.append(row)
    if len(unique_rows) < len(all_rows):
        print(f"Deduplicated: {len(all_rows)} → {len(unique_rows)} unique channel(s)")
    all_rows = unique_rows

    # ── Fetch channel activity (last upload, recent cadence) ─
    if all_rows:
        print(f"Fetching activity for {len(all_rows)} unique channel(s) (~{len(all_rows)} units)...")
        now_utc = datetime.now(timezone.utc)
        for row in all_rows:
            playlist_id = row.pop("_uploads_playlist_id", "")
            row.update(get_channel_activity(youtube, playlist_id, now_utc=now_utc))

    # ── Enrichment pipeline (optional) ───────────────────────
    enrichment_on = bool(enrichment_config and enrichment_config.enabled)
    enriched_records: list[EnrichedRecord] = []
    if enrichment_on and all_rows:
        relevant_ids = {row["channel_id"] for row in all_rows}
        relevant_resources = {cid: channel_resources[cid] for cid in relevant_ids
                              if cid in channel_resources}
        enrichment_dir = output_name or os.path.splitext(os.path.basename(output))[0]
        enrichment_output_dir = os.path.join(OUTPUTS_DIR, enrichment_dir)
        print(f"\nEnrichment: running pipeline on {len(relevant_resources)} unique channel(s)...")
        enriched_records = run_enrichment(
            youtube=youtube,
            channel_resources=relevant_resources,
            config=enrichment_config,
            output_dir=enrichment_output_dir,
        )

        # Apply wave filter (tier / limit) and reorder rows accordingly.
        kept = apply_wave_filter(enriched_records, enrichment_config)
        kept_order = {r.channel_id: i for i, r in enumerate(kept)}
        kept_ids = set(kept_order)
        before = len(all_rows)
        all_rows = [r for r in all_rows if r["channel_id"] in kept_ids]
        all_rows.sort(key=lambda r: kept_order.get(r["channel_id"], 10**9))
        if len(all_rows) < before:
            print(f"  Wave filter: {before} → {len(all_rows)} row(s) "
                  f"(tier={enrichment_config.tier or 'any'}, "
                  f"limit={enrichment_config.limit if enrichment_config.limit is not None else 'none'})")

        # Merge enrichment fields into rows. `contact_email` is unioned with
        # the row's existing YouTube-description-derived emails rather than
        # overwritten — see _merge_emails.
        records_by_id = {r.channel_id: r for r in enriched_records}
        for row in all_rows:
            rec = records_by_id.get(row["channel_id"])
            row.update(enrichment_row(rec))
            enrich_email = rec.contact_email if rec else ""
            row["contact_email"] = _merge_emails(row.get("contact_email", ""), enrich_email)

    # ── Write CSV ────────────────────────────────────────────
    if not all_rows:
        print("No results to write.")
        return

    config_keys = [
        "name", "keywords", "region_code", "language",
        "max_results", "order", "search_type",
        "min_subscribers", "max_subscribers",
    ]

    fields = order_csv_fields(
        CSV_FIELDS + (ENRICHMENT_FIELDS if enrichment_on else []),
        PRIORITY_FIELDS,
    )

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        # Config table: one header row + one row per search variant
        writer.writerow(["# config"] + config_keys)
        for s in searches:
            writer.writerow(["#"] + [s.get(k, "") for k in config_keys])
        writer.writerow(["# fetched_at", fetched_at])
        if enrichment_on:
            writer.writerow([
                "# enrichment",
                f"enabled={enrichment_config.enabled}",
                f"tech_detect={enrichment_config.tech_detect}",
                f"hunter={'on' if enrichment_config.hunter_api_key else 'off'}",
                f"tier={enrichment_config.tier or 'any'}",
                f"limit={enrichment_config.limit if enrichment_config.limit is not None else 'none'}",
            ])
        writer.writerow([])

        # Data
        dict_writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        dict_writer.writeheader()
        dict_writer.writerows(all_rows)

    print(f"\nDone. {len(all_rows)} row(s) written to '{output}'")
    if enrichment_on:
        print(f"Enrichment DB:  outputs/{enrichment_dir}/enrichment.db")


def run(config_path: str, search_name: str | None = None, dry_run: bool = False):
    cfg      = load_config(config_path)
    searches = cfg.get("searches", [])

    if not searches:
        sys.exit("No searches defined in config. Add at least one entry under 'searches:'.")

    # ── Filter to a single search if --search was provided ──
    if search_name:
        searches = [s for s in searches if s.get("name") == search_name]
        if not searches:
            available = ", ".join(s.get("name", "") for s in cfg.get("searches", []))
            sys.exit(
                f"ERROR: No search named '{search_name}' found in {config_path}.\n"
                f"Available names: {available}"
            )

    output_name = search_name or os.path.basename(cfg.get("output_csv", "results.csv")).removesuffix(".csv")
    run_searches(searches, output_name, dry_run)


def main():
    parser = argparse.ArgumentParser(description="YouTube channel searcher")
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to YAML config file (default: config.yaml)"
    )
    parser.add_argument(
        "--search", default=None, metavar="NAME",
        help="Run only the search block with this name (must match searches[].name in config)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print quota cost estimate without making API calls"
    )
    args = parser.parse_args()
    run(args.config, search_name=args.search, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
