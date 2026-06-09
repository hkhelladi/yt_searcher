"""SQLite-backed cache for EnrichedRecord — per-stage idempotency + resume.

The DB sits next to the final CSV (`outputs/<run_name>/enrichment.db`). Each
stage of the waterfall persists after it runs, so a crashed run can be resumed
without re-spending YouTube quota or re-crawling sites.

The HTML cache from M3 lives in-memory only — it's not persisted, since it's
large and only feeds M4/M5 within a single run.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from typing import Iterable, Iterator

from enrichment.schema import EnrichedRecord


# Map dataclass fields → SQLite column types. Anything serialised as JSON
# lands in a TEXT column; dates/datetimes go in TEXT (ISO 8601).
_COLUMN_TYPES: dict[str, str] = {
    "channel_id": "TEXT PRIMARY KEY",
    "channel_title": "TEXT",
    "yt_subscribers": "INTEGER",
    "yt_subscribers_hidden": "INTEGER",
    "yt_total_views": "INTEGER",
    "yt_video_count": "INTEGER",
    "yt_created_at": "TEXT",
    "yt_country": "TEXT",
    "yt_latest_video_at": "TEXT",
    "yt_avg_video_seconds": "INTEGER",
    "yt_median_video_seconds": "INTEGER",
    "yt_content_category_id": "TEXT",
    "yt_topic_categories": "TEXT",
    "content_type": "TEXT",
    "yt_channel_description": "TEXT",
    "yt_video_descriptions": "TEXT",
    "site_url": "TEXT",
    "site_resolved": "INTEGER",
    "site_final_url": "TEXT",
    "domain": "TEXT",
    "domain_created_at": "TEXT",
    "site_type": "TEXT",
    "site_is_dynamic": "INTEGER",
    "site_is_ecommerce": "INTEGER",
    "site_technologies": "TEXT",
    "has_affiliate_links": "INTEGER",
    "affiliate_networks": "TEXT",
    "site_sells_services": "INTEGER",
    "social_profiles": "TEXT",
    "traffic_rank": "INTEGER",
    "traffic_rank_source": "TEXT",
    "geo_best_guess": "TEXT",
    "geo_signals": "TEXT",
    "contact_email": "TEXT",
    "contact_source": "TEXT",
    "score": "REAL",
    "tier": "TEXT",
    "gate_failures": "TEXT",
    "compliance_flag": "TEXT",
    "enriched_at": "TEXT",
    "stages_completed": "TEXT",
}


def _persisted_field_names() -> list[str]:
    ephemeral = set(EnrichedRecord._EPHEMERAL)
    return [f.name for f in fields(EnrichedRecord) if f.name not in ephemeral]


class EnrichmentStore:
    """Thin wrapper over a single-table SQLite DB keyed on `channel_id`."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        cols_sql = ",\n  ".join(f"{name} {ctype}" for name, ctype in _COLUMN_TYPES.items())
        with self._conn() as conn:
            conn.execute(f"CREATE TABLE IF NOT EXISTS enriched (\n  {cols_sql}\n)")

    def upsert(self, record: EnrichedRecord) -> None:
        row = record.to_db_row()
        names = _persisted_field_names()
        placeholders = ", ".join(f":{n}" for n in names)
        cols = ", ".join(names)
        updates = ", ".join(f"{n}=excluded.{n}" for n in names if n != "channel_id")
        sql = (
            f"INSERT INTO enriched ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(channel_id) DO UPDATE SET {updates}"
        )
        with self._conn() as conn:
            conn.execute(sql, row)

    def upsert_many(self, records: Iterable[EnrichedRecord]) -> None:
        for r in records:
            self.upsert(r)

    def get(self, channel_id: str) -> EnrichedRecord | None:
        with self._conn() as conn:
            cur = conn.execute("SELECT * FROM enriched WHERE channel_id = ?", (channel_id,))
            row = cur.fetchone()
        return EnrichedRecord.from_db_row(dict(row)) if row else None

    def all(self) -> list[EnrichedRecord]:
        with self._conn() as conn:
            cur = conn.execute("SELECT * FROM enriched")
            rows = cur.fetchall()
        return [EnrichedRecord.from_db_row(dict(r)) for r in rows]
