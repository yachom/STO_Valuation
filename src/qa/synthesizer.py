"""Synthesizer: 여러 전문가 답을 하나의 최종 답변으로 합성."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..agents._llm import make_llm
from .session import QASession

SYSTEM = """당신은 여러 전문가 답변을 받아 사용자에게 보낼 최종 응답을 작성하는 합성가다.
원칙:
- 중복/모순 정리. 모순이 있으면 명시.
- 전문가가 1명뿐이면 그대로 다듬어 전달.
- 인용 흔적(섹션, 링크 등)은 보존.
- 한국어. 간결하면서 친절하게.
"""


def synthesize(
    question: str,
    specialist_answers: dict[str, str],
    session: QASession,
    router_meta: dict[str, Any] | None = None,
    callbacks: list[Any] | None = None,
) -> str:
    if len(specialist_answers) == 1:
        only = next(iter(specialist_answers.values()))
        return only.strip()

    blocks = "\n\n".join(
        f"## [{name}]\n{answer}" for name, answer in specialist_answers.items()
    )
    llm = make_llm(max_tokens=2000)
    body = (
        f"# 사용자 질문\n{question}\n\n"
        f"# 라우터 결정\n{router_meta or {}}\n\n"
        f"# 전문가별 답변\n{blocks}\n\n"
        "위 답변을 통합해 사용자에게 보낼 최종 응답을 마크다운으로 작성하라."
    )
    resp = llm.invoke(
        [SystemMessage(content=SYSTEM), HumanMessage(content=body)],
        config={"callbacks": callbacks or []},
    )
    return resp.content if isinstance(resp.content, str) else str(resp.content)
