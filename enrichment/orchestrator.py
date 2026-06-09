"""M1 — Enrichment orchestrator (waterfall + caching + persistence).

Walks each EnrichedRecord through M2 → M3 → M4 → M5 → M6 → M7 → M8, persisting
to the SQLite store after each stage so a crashed run can resume without re-
spending YouTube quota or re-crawling sites. Gated rows are still scored (not
dropped) so they show up in the audit CSV with their `gate_failures` populated.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Iterable

from enrichment.config import EnrichmentConfig, VIDEO_SAMPLE_SIZE
from enrichment.geo_contact import stage_geo_contact
from enrichment.patterns import stage_patterns
from enrichment.schema import EnrichedRecord
from enrichment.scoring import stage_score
from enrichment.site import stage_site
from enrichment.store import EnrichmentStore
from enrichment.tranco import stage_tranco
from enrichment.youtube import seed_from_resource, stage_youtube


def _needs(stage: str, records: list[EnrichedRecord]) -> list[EnrichedRecord]:
    return [r for r in records if stage not in r.stages_completed]


def _merge_seed_into_cached(cached: EnrichedRecord, seed: EnrichedRecord) -> EnrichedRecord:
    """When we have a cached record, prefer cached fields but keep the seed's
    raw text (descriptions) if cached lost them (HTML cache is ephemeral)."""
    if not cached.yt_channel_description and seed.yt_channel_description:
        cached.yt_channel_description = seed.yt_channel_description
    if not cached.channel_title and seed.channel_title:
        cached.channel_title = seed.channel_title
    return cached


async def _async_pipeline(
    records: list[EnrichedRecord],
    config: EnrichmentConfig,
    store: EnrichmentStore,
) -> list[EnrichedRecord]:
    # M3 — site discovery / crawl / RDAP
    todo = _needs("M3", records)
    if todo:
        print(f"  M3 site discovery: {len(todo)} channel(s)")
        await stage_site(todo)
        store.upsert_many(todo)

    # M4 — tech detection (opt-in)
    if config.tech_detect:
        todo = [r for r in _needs("M4", records) if r.site_resolved]
        if todo:
            print(f"  M4 tech detection: {len(todo)} site(s)")
            # Import here so importing the package doesn't require Wappalyzer
            from enrichment.tech import stage_tech
            await stage_tech(todo)
            store.upsert_many(todo)

    # M5 — pattern extraction (sync, but cheap to inline here)
    todo = _needs("M5", records)
    if todo:
        print(f"  M5 pattern extraction: {len(todo)} record(s)")
        for r in todo:
            stage_patterns(r)
        store.upsert_many(todo)

    # M6 — Tranco rank (sync)
    todo = _needs("M6", records)
    if todo:
        print(f"  M6 Tranco rank: {len(todo)} record(s)")
        stage_tranco(todo)
        store.upsert_many(todo)

    # M7 — geo + contact
    todo = _needs("M7", records)
    if todo:
        print(f"  M7 geo + contact: {len(todo)} record(s)")
        await stage_geo_contact(todo, config.hunter_api_key)
        store.upsert_many(todo)

    return records


def run_enrichment(
    youtube,
    channel_resources: dict[str, dict],
    config: EnrichmentConfig,
    output_dir: Path | str,
) -> list[EnrichedRecord]:
    """Public entry point. Takes a dict of `channel_id → channels.list resource`
    (each resource must have been fetched with
    `part=snippet,statistics,contentDetails,topicDetails,brandingSettings`),
    runs the full enrichment waterfall, and returns the final EnrichedRecord
    list. Persists to `enrichment.db` under `output_dir`.
    """

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    store = EnrichmentStore(output_dir / "enrichment.db")

    # ── Seed + hydrate from cache ────────────────────────────────────────
    seeds: list[EnrichedRecord] = []
    for cid, resource in channel_resources.items():
        seed = seed_from_resource(resource)
        cached = store.get(cid)
        if cached is not None:
            seeds.append(_merge_seed_into_cached(cached, seed))
        else:
            seeds.append(seed)

    # ── M2 — YouTube video sample ────────────────────────────────────────
    todo = _needs("M2", seeds)
    if todo:
        print(f"  M2 YouTube enrichment: {len(todo)} channel(s)")
        stage_youtube(
            youtube,
            todo,
            channel_resources,
            sample_size=VIDEO_SAMPLE_SIZE,
            exclude_shorts=config.exclude_shorts,
        )
        store.upsert_many(todo)

    # ── M3 .. M7 ─────────────────────────────────────────────────────────
    asyncio.run(_async_pipeline(seeds, config, store))

    # ── M8 — scoring (always re-run; cheap, depends on every prior field) ─
    print(f"  M8 scoring + gates: {len(seeds)} record(s)")
    stage_score(seeds, config)
    now = datetime.utcnow()
    for r in seeds:
        r.enriched_at = now
    store.upsert_many(seeds)

    return seeds


def apply_wave_filter(
    records: Iterable[EnrichedRecord],
    config: EnrichmentConfig,
) -> list[EnrichedRecord]:
    """Honor `enrichment.tier` and `enrichment.limit`. Sort by tier → score."""
    out = list(records)
    if config.tier:
        wanted = config.tier.upper()
        out = [r for r in out if r.tier == wanted]
    out.sort(key=lambda r: (("ABC".index(r.tier) if r.tier in "ABC" else 99), -r.score))
    if config.limit is not None and config.limit >= 0:
        out = out[: config.limit]
    return out
