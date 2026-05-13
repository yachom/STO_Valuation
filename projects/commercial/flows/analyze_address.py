"""Commercial address-analysis flow.

Composes:
  1. InitialCollectorService (deterministic public-API sequence)
  2. common.flows.exploration (LangGraph Planner→Gate→Retriever→Critic)
  3. CommercialGraphSignalWriter / OntologyMapper (graph upserts)
  4. common.flows.qa (final answer synthesis)
"""

from __future__ import annotations

from common.flows.exploration import ExplorationState
from projects.commercial.agents.critic import CommercialCritic
from projects.commercial.agents.planner import CommercialPlanner
from projects.commercial.agents.retriever import CommercialRetriever
from projects.commercial.domain.claims import AnalysisResult
from projects.commercial.services.initial_collector import InitialCollectorService
from projects.commercial.services.report_builder import CommercialReportBuilder


class AnalyzeAddressFlow:
    def __init__(
        self,
        initial: InitialCollectorService,
        planner: CommercialPlanner,
        retriever: CommercialRetriever,
        critic: CommercialCritic,
        report_builder: CommercialReportBuilder,
        max_iterations: int = 3,
    ) -> None:
        self._initial = initial
        self._planner = planner
        self._retriever = retriever
        self._critic = critic
        self._report = report_builder
        self._max_iterations = max_iterations

    def run(self, address: str) -> AnalysisResult:
        raise NotImplementedError

    def _initial_state(self, address: str) -> ExplorationState:
        raise NotImplementedError
