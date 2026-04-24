"""
run.py — YouTube Searcher launcher

Usage:
    python run.py run_configs/bc_mortgage_brokers_cities.yaml
    python run.py run_configs/bc_mortgage_brokers_cities.yaml --dry-run
    python run.py                                   # lists available run_configs

Each run_config YAML has:
    name:        <main run name>             → outputs/<name>_<ts>.csv
    base:        {...}                        → shared search config, runs first
    variations:  [{name, ...}, ...]           (optional) → additional searches

All searches (base + variations) write to a single CSV at
`outputs/<main_name>_<timestamp>.csv`. The top of the CSV is a "# config" block
with one row per search that ran (the full list of variations), followed by the
channel rows. Results are deduplicated by channel_id across all variations.
Each data row's `search_name` is `<main_name>__<variation_name>` so the source
variation is preserved even if the CSV is split or merged later.
"""

import argparse
import copy
import sys
from pathlib import Path

import yaml

import youtube_searcher

RUN_CONFIGS_DIR = "run_configs"


def load_run_config(path: Path) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        sys.exit(f"ERROR: {path} must be a YAML mapping at the top level.")
    for required in ("name", "base"):
        if required not in cfg:
            sys.exit(f"ERROR: {path} is missing required top-level field '{required}'.")
    if not isinstance(cfg["base"], dict):
        sys.exit(f"ERROR: {path} 'base' must be a mapping.")
    return cfg


def build_searches(cfg: dict) -> list[dict]:
    """Return a flat list of fully-merged search dicts: base first, then each variation."""
    main_name = cfg["name"]
    base = cfg["base"]
    variations = cfg.get("variations") or []

    searches: list[dict] = []

    base_search = copy.deepcopy(base)
    base_search["name"] = main_name
    searches.append(base_search)

    for i, variation in enumerate(variations, start=1):
        if not isinstance(variation, dict):
            sys.exit(f"ERROR: variation #{i} must be a mapping.")
        if "name" not in variation:
            sys.exit(f"ERROR: variation #{i} is missing required 'name'.")
        merged = copy.deepcopy(base)
        for k, v in variation.items():
            if k == "name":
                continue
            merged[k] = v
        merged["name"] = f"{main_name}__{variation['name']}"
        searches.append(merged)

    return searches


def list_available_configs() -> list[Path]:
    d = Path(RUN_CONFIGS_DIR)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.yaml"))


def print_usage_and_exit():
    print("Usage: python run.py <path-to-run-config-yaml> [--dry-run]\n")
    configs = list_available_configs()
    if configs:
        print(f"Available run_configs in '{RUN_CONFIGS_DIR}/':")
        for p in configs:
            print(f"  - {p}")
    else:
        print(f"No run_configs found in '{RUN_CONFIGS_DIR}/'.")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="YouTube Searcher — run a YAML run_config",
    )
    parser.add_argument("config", nargs="?", help="Path to a run_config YAML.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print quota estimate without making API calls",
    )
    args = parser.parse_args()

    if not args.config:
        print_usage_and_exit()

    config_path = Path(args.config)
    if not config_path.is_file():
        sys.exit(f"ERROR: config file not found: {config_path}")

    cfg = load_run_config(config_path)
    main_name = cfg["name"]
    searches = build_searches(cfg)

    print(f"Run config : {config_path}")
    print(f"Main run   : {main_name}")
    print(f"Searches   : {len(searches)}  (1 base + {len(searches) - 1} variation(s))")
    print(f"Output     : outputs/{main_name}_<timestamp>.csv")
    print()
    for s in searches:
        tag = "[base]" if s["name"] == main_name else "      "
        print(
            f"  {tag} {s['name']:<48}  "
            f"keywords='{s.get('keywords','')}'  "
            f"lang={s.get('language','')}"
        )

    youtube_searcher.run_searches(
        searches,
        output_name=main_name,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
