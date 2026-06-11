"""에이전트 공통 유틸리티 — AgentContext, 시스템 프롬프트 헬퍼.

각 에이전트는 함수로 구현되어 ReportState의 일부를 받아 일부를 갱신한다.
LLM 인스턴스는 `llm.get_llm("agent_name")` 1곳에서만 받음 — provider/Langfuse 분기는 BASE_LLM 매니저 내부에서 캡슐화.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from real_estate.report.tool.retriever.protocol import Retriever

logger = logging.getLogger("agent.base")


@dataclass
class AgentContext:
    """모든 노드에 공유되는 종속성. graph_v1.compile() 시 closure로 주입."""

    retriever: Retriever


DOMAIN_HIERARCHY = """
도메인 위계 (이걸 명시적으로 적용 — 등급 순서 환각 차단):

[BEEC 건물에너지효율등급] 높음 → 낮음
  1+++등급 > 1++등급 > 1+등급 > 1등급 > 2등급 > 3등급 > 4등급 > 5등급 > 6등급 > 7등급
  ('+' 수가 많을수록 우수. '1'은 같은 base지만 plus가 우선.)

[G-SEED 녹색건축인증] 높음 → 낮음
  최우수(그린1등급) > 우수(그린2등급) > 우량(그린3등급) > 일반(그린4등급)
  (그린N의 N이 작을수록 우수. '우수'가 '우량'보다 높음을 혼동 금지.)
  '우량 이상' = 우량+우수+최우수 (3등급 합산).
  '우수 이상' = 우수+최우수 (2등급 합산).

[부동산 가치 vs 사회적 가치] 본 평가는 '사회적 가치' 중심.
  - 임차 사업자 안정성, 상권 건강도, 입지 접근성, 배후수요는 모두 사회적 가치 측면.
  - 단순 시세·매도가격은 평가의 부산물이며 핵심 결론이 아님.
"""


def korean_system_prompt(role_description: str) -> str:
    return (
        "당신은 한국 상가 부동산의 사회적 가치를 평가하는 보고서 에이전트입니다.\n"
        "평가 대상은 '상가 건물(주소 단위)'이며, 아파트 단지 데이터는 "
        "본 상가의 '배후수요(잠재 소비층)' 측정 컨텍스트로만 활용합니다.\n"
        f"이번 역할: {role_description}\n"
        "원칙:\n"
        "- 한국어로 답합니다.\n"
        "- 모든 주장은 evidence에서 끌어와야 합니다. evidence 밖의 사실은 추측하지 마세요.\n"
        "- 숫자는 evidence에 있는 값만 인용합니다.\n"
        "- 출처가 부족하면 그렇다고 명시하세요.\n"
        "- 도메인 위계(아래)를 무시하고 등급/순서를 임의로 추론하지 마세요.\n"
        + DOMAIN_HIERARCHY
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
