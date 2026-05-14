"""뉴스 수집 → 태깅 → 그래프 적재."""

from __future__ import annotations

from typing import Any

from ..kg.graph_store import KnowledgeGraph
from ..kg.tagger import tag_articles
from .crawler import fetch_news


def asset_context_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    addr = metadata.get("address", {}) or {}
    return {
        "asset_name": addr.get("building_name") or addr.get("road"),
        "road": addr.get("road"),
        "jibun": addr.get("jibun"),
        "sigungu": addr.get("sigungu"),
        "bjdong": addr.get("bjdong_name"),
        "pnu": (metadata.get("identifiers") or {}).get("pnu"),
    }


def run(metadata: dict[str, Any], months: int = 12, max_tag: int = 30, callbacks: list[Any] | None = None) -> dict[str, Any]:
    news = fetch_news(metadata, months=months)
    context = asset_context_from_metadata(metadata)

    tagged = tag_articles(context, news.get("items", []), callbacks=callbacks, max_articles=max_tag) if news.get("items") else []

    kg = KnowledgeGraph()
    ingest_stats = kg.ingest_tagged(context, tagged)

    return {
        "asset_context": context,
        "fetch": {
            "queries": news.get("queries"),
            "total_unique": news.get("total_unique"),
            "errors": news.get("errors"),
            "skipped": news.get("skipped"),
        },
        "tagged_count": sum(1 for r in tagged if (r.get("tagged") or {}).get("asset_relevance") not in ("none", None)),
        "graph": kg,
        "graph_summary": kg.summary(),
        "ingest_stats": ingest_stats,
    }
