"""Commercial OntologyTagger — implements common.ports.news.OntologyTagger."""

from __future__ import annotations

from typing import Any

from common.ports.llm import LLMClient
from common.ports.news import OntologyTagger
from common.ports.repository import NormalizedNews
from projects.commercial.domain.ontology import OntologyTag


class CommercialOntologyTagger(OntologyTagger):
    asset_class = "commercial"

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def tag(self, news: NormalizedNews) -> list[dict[str, Any]]:
        """Return one or more OntologyTag dicts. Empty list if not relevant."""
        raise NotImplementedError

    def _build_prompt(self, news: NormalizedNews) -> str:
        raise NotImplementedError

    def _parse(self, text: str) -> list[OntologyTag]:
        raise NotImplementedError
