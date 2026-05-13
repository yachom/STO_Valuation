"""Asset-supplied interfaces for the (common) news pipeline.

The common news pipeline orchestrates normalize → filter → dedup → tag → write
without knowing the asset's ontology. The asset supplies the two pieces of
behavior that *do* depend on its ontology:

  - OntologyTagger: text → asset-specific tag(s)
  - GraphSignalWriter: tag + news → graph nodes/edges

Each ``projects/<asset>/services/`` provides concrete implementations.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from common.ports.repository import NormalizedNews


@runtime_checkable
class OntologyTagger(Protocol):
    """Map normalized news to asset-specific ontology tag(s)."""

    asset_class: str

    def tag(self, news: NormalizedNews) -> list[dict[str, Any]]:
        """Return a list of tag dicts (shape defined by the asset)."""


@runtime_checkable
class GraphSignalWriter(Protocol):
    """Persist (news, tag) into the graph as nodes/edges + a MarketSignal."""

    asset_class: str

    def write(self, news: NormalizedNews, tags: list[dict[str, Any]]) -> None: ...
