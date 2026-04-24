#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# run.sh — YouTube Searcher launcher
#
# Usage:
#   ./run.sh <search_name> [config_file] [--dry-run]
#
# Examples:
#   ./run.sh ca_mortgage_brokers_en
#   ./run.sh ca_mortgage_brokers_en examples/ca_mortgage_brokers.yaml
#   ./run.sh ca_mortgage_brokers_en examples/ca_mortgage_brokers.yaml --dry-run
#
# <search_name> must match a searches[].name value in the config file.
# Output is written to outputs/<search_name>.csv
# ─────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Args ─────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
  echo "Usage: ./run.sh <search_name> [config_file] [--dry-run]"
  echo ""
  echo "Available searches in config.yaml:"
  grep "^ *- name:" config.yaml | sed "s/.*name: /  - /" || true
  echo ""
  echo "Available searches in examples/:"
  grep -rh "^ *- name:" examples/ | sed "s/.*name: /  - /" | sort -u || true
  exit 1
fi

SEARCH_NAME="$1"
shift

# Second arg is config file if it doesn't start with --
CONFIG="config.yaml"
if [[ $# -gt 0 && "$1" != --* ]]; then
  CONFIG="$1"
  shift
fi

# Remaining args forwarded to Python (e.g. --dry-run)
EXTRA_ARGS=("$@")

# ── Python / venv ─────────────────────────────────────────────
if [[ -f ".venv/bin/python" ]]; then
  PYTHON=".venv/bin/python"
elif [[ -f "venv/bin/python" ]]; then
  PYTHON="venv/bin/python"
else
  PYTHON="python3"
fi

# ── Run ───────────────────────────────────────────────────────
echo "Config  : $CONFIG"
echo "Search  : $SEARCH_NAME"
echo "Output  : outputs/${SEARCH_NAME}.csv"
echo ""

"$PYTHON" youtube_searcher.py \
  --config "$CONFIG" \
  --search "$SEARCH_NAME" \
  "${EXTRA_ARGS[@]}"
