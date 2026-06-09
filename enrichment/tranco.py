"""M6 — Tranco traffic-rank lookup.

Downloads the latest Tranco top-1M list (free) once per ISO week, caches it
under ~/.cache/youtube_searcher/, and looks up each record's domain. Absence
from the list is normal for creator sites — interpret as "below top 1M",
not an error.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterable

import httpx

from enrichment.config import HTTP_TIMEOUT_S, USER_AGENT
from enrichment.schema import EnrichedRecord


TRANCO_URL = "https://tranco-list.eu/top-1m.csv.zip"


def _cache_dir() -> Path:
    p = Path.home() / ".cache" / "youtube_searcher"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_file_for_today() -> Path:
    today = date.today().isocalendar()  # (year, week, weekday)
    return _cache_dir() / f"tranco_{today.year}-W{today.week:02d}.csv.zip"


def _download_tranco(dest: Path) -> bool:
    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=60,  # the file is ~15 MB, allow more than HTTP_TIMEOUT_S
            follow_redirects=True,
        ) as client:
            resp = client.get(TRANCO_URL)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        return True
    except (httpx.HTTPError, OSError):
        return False


def load_tranco_index(target_domains: Iterable[str] | None = None) -> dict[str, int]:
    """Return {domain: rank}. If `target_domains` is provided we filter while
    streaming the CSV — keeps memory tiny for the few hundred lookups we need.
    Falls back to an empty dict if download fails."""
    cache_path = _cache_file_for_today()
    if not cache_path.exists():
        if not _download_tranco(cache_path):
            return {}

    targets = {d.lower() for d in target_domains} if target_domains is not None else None
    index: dict[str, int] = {}
    try:
        with zipfile.ZipFile(cache_path) as zf:
            # The archive contains a single CSV; pick the first.
            name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
            if not name:
                return {}
            with zf.open(name) as raw:
                stream = io.TextIOWrapper(raw, encoding="utf-8", errors="ignore")
                reader = csv.reader(stream)
                for row in reader:
                    if len(row) < 2:
                        continue
                    rank_s, domain = row[0], row[1].strip().lower()
                    if targets is not None and domain not in targets:
                        continue
                    try:
                        index[domain] = int(rank_s)
                    except ValueError:
                        continue
                    if targets is not None and len(index) == len(targets):
                        break
    except (zipfile.BadZipFile, OSError):
        return {}

    return index


def _domain_variants(domain: str) -> list[str]:
    """Tranco indexes registrable domains. Try the exact host and the bare
    apex (drop subdomains) so 'blog.example.com' still matches 'example.com'."""
    domain = (domain or "").lower().removeprefix("www.")
    if not domain:
        return []
    variants = [domain]
    parts = domain.split(".")
    if len(parts) > 2:
        variants.append(".".join(parts[-2:]))
    return variants


def stage_tranco(records: list[EnrichedRecord]) -> list[EnrichedRecord]:
    if not records:
        return records

    domains = {d for r in records if r.domain for d in _domain_variants(r.domain)}
    if not domains:
        return records

    index = load_tranco_index(domains)
    for r in records:
        if not r.domain:
            continue
        for variant in _domain_variants(r.domain):
            if variant in index:
                r.traffic_rank = index[variant]
                r.traffic_rank_source = "tranco"
                break
        if "M6" not in r.stages_completed:
            r.stages_completed.append("M6")
    return records
