"""Enrichment pipeline package.

Takes the deduplicated channel rows produced by `youtube_searcher.run_searches`
and walks them through M2..M9 (per the implementation spec), populating an
EnrichedRecord per channel. The orchestrator is the public entry point.
"""

from enrichment.config import EnrichmentConfig
from enrichment.orchestrator import run_enrichment
from enrichment.schema import EnrichedRecord

__all__ = ["EnrichmentConfig", "EnrichedRecord", "run_enrichment"]
