"""ReportAnalyst: 생성된 보고서 본문을 컨텍스트로 답변."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ...agents._llm import make_llm
from ..session import QASession

SYSTEM = """당신은 부동산 STO 사회적 가치 평가 보고서에 대한 질의응답을 담당하는 애널리스트다.
주어진 보고서 본문을 근거로 사용자 질문에 답하라.

원칙:
- 보고서에 있는 내용만 인용/요약하라. 없는 내용은 '보고서에 명시되지 않음'으로 답하라.
- 인용할 때는 보고서 섹션 번호나 표 항목명을 함께 표기하라.
- 한국어. 간결하지만 근거 충실히.
"""


def run(question: str, session: QASession, callbacks: list[Any] | None = None) -> str:
    llm = make_llm(max_tokens=1500)
    body = (
        f"# 보고서 본문\n```markdown\n{session.report_excerpt()}\n```\n\n"
        f"# 대화 히스토리\n{session.history_excerpt() or '(없음)'}\n\n"
        f"# 질문\n{question}"
    )
    resp = llm.invoke(
        [SystemMessage(content=SYSTEM), HumanMessage(content=body)],
        config={"callbacks": callbacks or []},
    )
    return resp.content if isinstance(resp.content, str) else str(resp.content)
