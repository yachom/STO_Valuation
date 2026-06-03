"""에이전트 공통 유틸리티 — AgentContext, 시스템 프롬프트 헬퍼.

각 에이전트는 함수로 구현되어 ReportState의 일부를 받아 일부를 갱신한다.
구체 ChatAnthropic은 build_llm() 통해서만 받음 → 모델 교체 시 한 곳만 수정.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from real_estate.report.rag.protocol import Retriever

logger = logging.getLogger("agent.base")


@dataclass
class AgentContext:
    """모든 노드에 공유되는 종속성. graph_v1.compile() 시 closure로 주입."""

    retriever: Retriever


def korean_system_prompt(role_description: str) -> str:
    return (
        "당신은 한국 부동산 자산의 사회적 가치를 평가하는 보고서 에이전트입니다.\n"
        f"이번 역할: {role_description}\n"
        "원칙:\n"
        "- 한국어로 답합니다.\n"
        "- 모든 주장은 evidence에서 끌어와야 합니다. evidence 밖의 사실은 추측하지 마세요.\n"
        "- 숫자는 evidence에 있는 값만 인용합니다.\n"
        "- 출처가 부족하면 그렇다고 명시하세요.\n"
    )


def format_snippets(snippets) -> str:
    """GroundedSnippet list → LLM 컨텍스트 문자열. 각 evidence에 [E#] 인덱스 부여."""
    if not snippets:
        return "(관련 evidence 없음)"
    parts = []
    for i, s in enumerate(snippets):
        ref_summary = ",".join(s.node_refs[:3])
        parts.append(
            f"[E{i+1}] (score={s.score:.2f}, refs={ref_summary}...)\n{s.text}"
        )
    return "\n\n".join(parts)
