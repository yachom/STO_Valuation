"""Asset-agnostic news pipeline orchestrator.

Stages (per product.md §5.1.1):
  search → normalize → save (dedup-by-url) → rule filter → embed → semantic dedup
  → ontology tagging → graph signal write

Asset-specific pieces are *injected* (not imported):
  - tagger:        OntologyTagger      (projects/<asset>/services/ontology_tagging.py)
  - graph_signal:  GraphSignalWriter   (projects/<asset>/services/graph_signal.py)
"""

from __future__ import annotations

from datetime import date
from typing import Iterable

from common.clients.naver_news import NaverNewsClient
from common.domain.asset import AssetProfile
from common.ports.embedding import EmbeddingClient
from common.ports.news import GraphSignalWriter, OntologyTagger
from common.ports.repository import NewsRepository
from common.services.news.filter_rules import Rule


def run(
    profile: AssetProfile,
    *,
    target_date: date,
    queries: Iterable[str],
    rules: list[Rule],
    news_client: NaverNewsClient,
    news_repo: NewsRepository,
    embedding: EmbeddingClient,
    tagger: OntologyTagger,
    graph_signal: GraphSignalWriter,
    semantic_dedup_threshold: float = 0.92,
) -> dict[str, int]:
    """Run one day of collection. Returns per-stage counters."""
    raise NotImplementedError
