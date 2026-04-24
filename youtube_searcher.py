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
from datetime import datetime, timezone
from math import ceil

import yaml
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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

# CSV output columns (in order)
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
    # ── Contact / business info ──
    "emails",
    "websites",
    "phone_numbers",
    "description_snippet",
    # ── Meta ──
    "fetched_at",
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
    """Calls channels.list in batches of 50 to get full channel details."""
    results = []
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i : i + 50]
        resp = youtube.channels().list(
            part="snippet,statistics,brandingSettings,contentDetails",
            id=",".join(batch),
            maxResults=50,
        ).execute()
        results.extend(resp.get("items", []))
    return results


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
        "emails":             emails,
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


OUTPUTS_DIR = "outputs"



def run_searches(
    searches: list[dict],
    output_name: str | None = None,
    dry_run: bool = False,
    output_path: str | None = None,
    show_quota: bool = True,
):
    """Run a list of search config dicts and write results to a single CSV.

    This is the core execution function — it takes ready-to-go search dicts
    rather than loading from a config file. Used by both run() and batch mode.

    Either `output_name` (CSV goes to outputs/{output_name}_{ts}.csv) or
    `output_path` (explicit file path) must be provided.
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
        print(f"\n  TOTAL THIS RUN : {total_units} units")
        print( "  Free daily quota: 10,000 units")
        print(f"  Remaining after : {10_000 - total_units} units  (approx)")
        if total_units > 10_000:
            print("  ⚠  Exceeds free daily quota — enable billing or reduce max_results.")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

    if dry_run:
        print("Dry-run mode: no API calls made.")
        return

    # ── Execute searches ─────────────────────────────────────
    youtube    = build("youtube", "v3", developerKey=api_key)
    all_rows   = []
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

    # ── Write CSV ────────────────────────────────────────────
    if not all_rows:
        print("No results to write.")
        return

    config_keys = [
        "name", "keywords", "region_code", "language",
        "max_results", "order", "search_type",
        "min_subscribers", "max_subscribers",
    ]

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        # Config table: one header row + one row per search variant
        writer.writerow(["# config"] + config_keys)
        for s in searches:
            writer.writerow(["#"] + [s.get(k, "") for k in config_keys])
        writer.writerow(["# fetched_at", fetched_at])
        writer.writerow([])

        # Data
        dict_writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        dict_writer.writeheader()
        dict_writer.writerows(all_rows)

    print(f"\nDone. {len(all_rows)} row(s) written to '{output}'")


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
