# YouTube Channel Searcher

Searches YouTube for channels matching configurable criteria and exports results to CSV. Built for prospecting and outreach — finds creators in a given niche and extracts their contact info.

Uses the **YouTube Data API v3**:
- Search endpoint: https://developers.google.com/youtube/v3/docs/search/list
- Channels endpoint: https://developers.google.com/youtube/v3/docs/channels/list
- Full API reference: https://developers.google.com/youtube/v3/docs

## Setup

### 1. Get an API key

1. Go to https://console.cloud.google.com/
2. Create a project (or select an existing one)
3. **APIs & Services > Library** — search "YouTube Data API v3" and enable it
4. **APIs & Services > Credentials > Create Credentials > API Key**
5. Copy the key

### 2. Configure the key

```bash
cp .env.example .env
# Edit .env and paste your key:
# YOUTUBE_API_KEY=AIza...
```

The key is read from the `YOUTUBE_API_KEY` environment variable (via `.env` or shell export). It is never stored in config files.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

## Usage

Runs are driven by a YAML file in `run_configs/`. Each YAML defines a **base** search and an optional list of **variations** that inherit the base and override specific fields (keywords, language, order, …).

```bash
# Run a config (base + all variations → one CSV)
python run.py run_configs/bc_mortgage_brokers_cities.yaml

# Preview quota without calling the API
python run.py run_configs/bc_mortgage_brokers_cities.yaml --dry-run

# Print usage and list available run_configs
python run.py
```

All searches (base + variations) are merged into a single CSV at `outputs/<main_name>_<timestamp>.csv`. The top of the file is a `# config` block listing every search that ran; data rows below are deduplicated by `channel_id`. Each row's `search_name` is `<main_name>__<variation_name>`, so the source variation is preserved even if CSVs are split or merged later.

### Run config schema

```yaml
name: bc_mortgage_brokers_cities        # main run name — drives the output filename

base:                                    # shared defaults, also runs as the first search
  keywords: "british columbia mortgage agent"
  region_code: CA
  language: en
  max_results: 50
  order: relevance
  search_type: channel
  min_subscribers: 100
  max_subscribers: null

variations:                              # optional — each is an additional search
  - {name: vancouver,  keywords: "mortgage agent vancouver"}
  - {name: victoria,   keywords: "mortgage agent victoria"}
  - {name: montreal,   keywords: "courtier hypotheque montreal", language: fr}
```

Each variation must have a `name`. Any other field is optional and falls back to `base`. A single YAML can therefore mix languages, regions, and orderings — the `montreal` variation above overrides `language` to `fr` while inheriting everything else from `base`.

### Enrichment (optional)

The search alone gives you the channel snapshot YouTube exposes — subscribers, recent uploads, the description. The **enrichment pipeline** layers on outreach signals: the creator's own website, who their tech stack suggests they are, affiliate networks they're using, services they sell, social profiles, traffic rank, a geo best-guess, a contact email, and a final score / tier. It's the same channel rows — just with extra columns.

Enable it by adding an `enrichment:` block at the top level of a run_config:

```yaml
name: bc_mortgage_brokers_cities

enrichment:
  enabled: true            # default: false. Flip to off to keep the legacy CSV unchanged.
  tech_detect: false       # M4 — requires `pip install python-Wappalyzer`. Off by default.
  hunter_api_key: ""       # M7 — optional Hunter.io fallback when no email is on the site.
  limit: null              # Wave cap: top-N rows after tier/score sort. null = no cap.
  tier: null               # Wave filter: only export rows in this tier ("A"|"B"|"C"). null = all.
  exclude_shorts: true     # Drop videos <60s from the avg-duration calculation.

base:
  keywords: "british columbia mortgage agent"
  region_code: CA
  ...
```

CLI flags override the YAML for the same keys: `--enrich` / `--no-enrich`, `--tier A`, `--limit 100`.

When enrichment is on, the same `outputs/<run_name>_<timestamp>.csv` is written with ~25 extra columns appended (`site_url`, `domain`, `domain_created_at`, `site_type`, `has_affiliate_links`, `affiliate_networks`, `site_sells_services`, `social_profiles`, `traffic_rank`, `geo_best_guess`, `contact_email`, `score`, `tier`, `gate_failures`, `compliance_flag`, …). A SQLite cache is also written to `outputs/<run_name>/enrichment.db` and reused across runs of the same config — re-running skips stages already completed for each channel, so a crashed mid-run is resumable without burning fresh quota.

Gated rows (inactive / no_site / no_email) are kept in the CSV and tagged in the `gate_failures` column rather than dropped — the pipeline produces a contact list, never sends. Rows with `geo_best_guess: CA` get `compliance_flag: CASL_REVIEW` (Canada's anti-spam law is stricter than US CAN-SPAM; verify your consent basis before any send).

The enrichment pipeline depends on `httpx` and `isodate` (installed by `pip install -r requirements.txt`). Tech detection (`tech_detect: true`) additionally requires `pip install python-Wappalyzer` — keep it off if you don't need the `site_type` / `site_is_ecommerce` / `site_is_dynamic` columns.

### Alternative: direct CLI

`youtube_searcher.py` can still be invoked directly against `config.yaml` (the flat list of named searches, no base/variation model):

```bash
# Run one named search from config.yaml
python youtube_searcher.py --search ca_mortgage_brokers_en

# Run every search defined in config.yaml
python youtube_searcher.py --config config.yaml

# Quota estimate only
python youtube_searcher.py --search ca_mortgage_brokers_en --dry-run
```

## Config parameters

Both `run_configs/*.yaml` (under `base:` or `variations[]`) and `config.yaml` (under `searches:`) accept the same parameters:

### `name`

A label you choose to identify this search. It has no effect on the API call. It is used to:
- In a run_config: the top-level `name:` becomes the output filename (`outputs/<name>_<timestamp>.csv`), and each variation's `name:` tags its rows as `<main_name>__<variation_name>` in the CSV
- In `config.yaml`: select which search to run via `--search` on the CLI, and name the output file

### `keywords`

**Maps to:** [`q` parameter](https://developers.google.com/youtube/v3/docs/search/list#q) in `search.list`

The search query string. YouTube matches this against channel names, descriptions, and video metadata. This is the primary lever for finding relevant creators.

Use natural phrases the way you would type into YouTube's search bar. Different phrasings surface different results — for example `"mortgage broker canada"` and `"courtier hypothécaire canada"` will return different channels, which is why the config supports multiple search blocks.

### `region_code`

**Maps to:** [`regionCode` parameter](https://developers.google.com/youtube/v3/docs/search/list#regionCode) in `search.list`

An [ISO 3166-1 alpha-2](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2) country code (e.g. `CA`, `US`, `FR`, `GB`, `AU`). Instructs the API to return results that would be available in that country's version of YouTube.

This is a **content availability filter**, not a geolocation filter — it does not guarantee the creator is physically located in that country. A Canadian creator whose content is available worldwide will still appear in `US` results. Combine with `keywords` that include the country name for best targeting.

### `language`

**Maps to:** [`relevanceLanguage` parameter](https://developers.google.com/youtube/v3/docs/search/list#relevanceLanguage) in `search.list`

An [ISO 639-1](https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes) two-letter language code (e.g. `en`, `fr`, `es`, `de`). This is a **hint** that tells YouTube to prefer results most relevant to speakers of that language. It biases ranking but does not hard-filter — you may still get results in other languages.

### `max_results`

**Controls:** how many pages of `search.list` are fetched

The total number of channel results to request from the API. Each API page returns up to 50 results, so `max_results: 200` makes 4 API calls. This is the main quota cost driver.

YouTube often returns diminishing quality past ~200 results for niche queries. Multiples of 50 are most quota-efficient since partial pages still cost a full API call.

### `order`

**Maps to:** [`order` parameter](https://developers.google.com/youtube/v3/docs/search/list#order) in `search.list`

How YouTube sorts the results before returning them. Accepted values:

| Value | What it does |
|---|---|
| `relevance` | **(default)** Best match to the query — usually the best starting point |
| `viewCount` | Channels with the most total views first — surfaces established creators |
| `videoCount` | Channels with the most uploads first — surfaces active/prolific creators |
| `rating` | Channels with higher-rated content first |
| `date` | Most recently active channels first — surfaces newer or recently active creators |

### `search_type`

**Maps to:** [`type` parameter](https://developers.google.com/youtube/v3/docs/search/list#type) in `search.list`

The kind of YouTube entity to search for. Accepted values: `channel`, `video`, `playlist`.

For prospecting, use `channel` — it searches directly for channel names and descriptions. If set to `video`, the tool still extracts the parent channel of each matching video, but you may get duplicate channels and noisier results.

### `min_subscribers`

**Applied after the API call** (no quota cost). Filters out channels with fewer subscribers than this value. Set to `0` or `null` to disable.

Useful for excluding inactive or very small channels that may not be worth contacting. Note: some channels hide their subscriber count — these are kept in results regardless of this filter since the count is unknown.

### `max_subscribers`

**Applied after the API call** (no quota cost). Filters out channels with more subscribers than this value. Set to `null` to disable.

Useful if you want to target mid-tier creators and exclude large media channels that are unlikely to respond to outreach.

## API quota and costs

The YouTube Data API v3 provides a **free daily quota of 10,000 units** (resets at midnight Pacific Time).

| API call | Cost | When it runs |
|---|---|---|
| `search.list` | **100 units** per call | Once per 50 results requested |
| `channels.list` | **1 unit** per call | Once per 50 channels to enrich |
| `playlistItems.list` | **1 unit** per call | Once per **unique** channel (after dedup) to fetch `last_upload_at` and recent cadence |

Cost examples per search block:

| `max_results` | `search.list` calls | `channels.list` calls | Total units |
|---|---|---|---|
| 50 | 1 | 1 | 101 |
| 100 | 2 | 2 | 202 |
| 200 | 4 | 4 | 404 |
| 500 | 10 | 10 | 1,010 |

Use `--dry-run` to see the exact quota estimate before making any API calls.

If you exceed 10,000 units/day, requests return HTTP 403 until midnight PT. Paid quota can be enabled through GCP billing at roughly $0.20 per 10,000 units.

Quota documentation: https://developers.google.com/youtube/v3/getting-started#quota

## CSV output

Each CSV file starts with metadata rows (prefixed with `#`) listing the full search configuration used — one row per search that ran, so a run_config with 20 variations produces 20 `#` config rows plus the base. The data header and result rows follow. Results are deduplicated by `channel_id` across all variations.

### Output columns

| Column | Source | Description |
|---|---|---|
| `search_name` | config | Name of the search block that produced this row |
| `keywords` | config | Query string used |
| `region_code` | config | Country code used |
| `language` | config | Language hint used |
| `order` | config | Sort order used |
| `search_type` | config | Entity type searched |
| `channel_name` | API | Display name of the channel |
| `channel_id` | API | Unique YouTube channel ID |
| `custom_url` | API | Vanity URL handle (e.g. `@channelname`) |
| `channel_url` | derived | Full clickable URL to the channel |
| `country` | API | Country the channel has set in their profile (may be empty) |
| `published_at` | API | When the channel was created |
| `subscribers` | API | Subscriber count (or `hidden` if the creator opted out) |
| `total_views` | API | Lifetime view count across all videos |
| `video_count` | API | Number of public videos uploaded |
| `last_upload_at` | API | Date of most recent video (`YYYY-MM-DD`); empty if no uploads |
| `days_since_last_upload` | derived | Days between now and `last_upload_at` — best "is this channel alive?" signal |
| `uploads_last_6mo` | derived | Count of videos uploaded in the last 180 days (capped at 50) — cadence indicator |
| `emails` | extracted | Email addresses found in the channel description |
| `websites` | extracted | Non-YouTube URLs found in the channel description |
| `phone_numbers` | extracted | Phone numbers found in the channel description |
| `description_snippet` | API | First 300 characters of the channel description |
| `fetched_at` | runtime | UTC timestamp of when this data was fetched |

Contact info (emails, websites, phone numbers) is regex-extracted from the channel's description text. Most business-oriented creators include a contact email there.

## Project structure

```
youtube_searcher/
  .env.example          # Template for API key
  .env                  # Your actual key (git-ignored)
  .gitignore
  config.yaml           # Flat list of named searches (used by youtube_searcher.py CLI)
  run_configs/          # Per-campaign YAMLs: base + variations → single CSV
  examples/             # Standalone config files per niche/geo
  outputs/              # CSV results (git-ignored, auto-created)
  requirements.txt
  run.py                # Launcher: python run.py run_configs/<file>.yaml
  run.sh                # Shell alternative to run.py
  youtube_searcher.py   # Core search logic
```
