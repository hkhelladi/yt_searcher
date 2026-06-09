"""EnrichedRecord — canonical schema shared by every stage.

Each pipeline stage reads an EnrichedRecord, mutates the fields it owns, and
hands it back to the orchestrator. SQLite serialization round-trips through
`to_db_row` / `from_db_row`; the CSV exporter reads the same fields directly.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, ClassVar


def _iso(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value.isoformat()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class EnrichedRecord:
    # Identity
    channel_id: str
    channel_title: str = ""

    # M2 YouTube
    yt_subscribers: int | None = None
    yt_subscribers_hidden: bool = False
    yt_total_views: int = 0
    yt_video_count: int = 0
    yt_created_at: date | None = None
    yt_country: str | None = None
    yt_latest_video_at: date | None = None
    yt_avg_video_seconds: int | None = None
    yt_median_video_seconds: int | None = None
    yt_content_category_id: str | None = None
    yt_topic_categories: list[str] = field(default_factory=list)
    content_type: str = "other"

    # Raw text kept so M3/M5 can re-scan without another API call
    yt_channel_description: str = ""
    yt_video_descriptions: list[str] = field(default_factory=list)

    # M3 site discovery
    site_url: str | None = None
    site_resolved: bool = False
    site_final_url: str | None = None
    domain: str | None = None
    domain_created_at: date | None = None
    site_html_cache: dict[str, str] = field(default_factory=dict)  # path -> html, in-memory only

    # M4 tech detection
    site_type: str | None = None
    site_is_dynamic: bool | None = None
    site_is_ecommerce: bool | None = None
    site_technologies: list[str] = field(default_factory=list)

    # M5 pattern extraction
    has_affiliate_links: bool = False
    affiliate_networks: list[str] = field(default_factory=list)
    site_sells_services: bool = False
    social_profiles: dict[str, str] = field(default_factory=dict)

    # M6 traffic
    traffic_rank: int | None = None
    traffic_rank_source: str | None = None

    # M7 geo + contact
    geo_best_guess: str = "UNKNOWN"
    geo_signals: dict[str, Any] = field(default_factory=dict)
    contact_email: str | None = None
    contact_source: str | None = None

    # M8 scoring
    score: float = 0.0
    tier: str = "C"
    gate_failures: list[str] = field(default_factory=list)
    compliance_flag: str = "OK"

    # Meta
    enriched_at: datetime | None = None
    stages_completed: list[str] = field(default_factory=list)

    # ── Serialization ──────────────────────────────────────────────────────

    # Fields that should not be persisted to SQLite (HTML cache is large + ephemeral).
    # ClassVar so dataclasses treats this as class-level config, not a field.
    _EPHEMERAL: ClassVar[tuple[str, ...]] = ("site_html_cache",)

    def to_db_row(self) -> dict[str, Any]:
        row = asdict(self)
        for k in self._EPHEMERAL:
            row.pop(k, None)
        # JSON-encode complex types; date/datetime → ISO string
        for k, v in list(row.items()):
            if isinstance(v, (list, dict)):
                row[k] = json.dumps(v, default=str)
            elif isinstance(v, (date, datetime)):
                row[k] = _iso(v)
        return row

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> EnrichedRecord:
        data = dict(row)

        # Decode JSON columns
        for k in ("yt_topic_categories", "yt_video_descriptions",
                  "site_technologies", "affiliate_networks",
                  "social_profiles", "geo_signals", "gate_failures",
                  "stages_completed"):
            if k in data and isinstance(data[k], str):
                try:
                    data[k] = json.loads(data[k])
                except (json.JSONDecodeError, TypeError):
                    pass

        # Decode dates
        for k in ("yt_created_at", "yt_latest_video_at", "domain_created_at"):
            if k in data:
                data[k] = _parse_date(data[k])
        if "enriched_at" in data:
            data["enriched_at"] = _parse_datetime(data["enriched_at"])

        # Coerce booleans from SQLite ints
        for k in ("yt_subscribers_hidden", "site_resolved",
                  "site_is_dynamic", "site_is_ecommerce",
                  "has_affiliate_links", "site_sells_services"):
            if k in data and data[k] is not None and not isinstance(data[k], bool):
                data[k] = bool(data[k])

        # HTML cache is ephemeral
        data.pop("site_html_cache", None)
        valid = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in valid})
