"""GraphQuery: 뉴스 지식그래프에서 엔티티/관계/축 시그널 조회 후 답변."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ...agents._llm import make_llm
from ..session import QASession

SYSTEM = """당신은 뉴스 기반 지식그래프(KG) 질의 전문가다.
KG 검색 결과(노드/엣지/축 시그널)를 보고 사용자 질문에 답하라.

원칙:
- KG에 있는 내용만 답하라. 추측 금지.
- 출처 기사 제목·링크·발행일을 함께 제시하라.
- 관계 표기 시 온톨로지 명칭을 그대로 쓰라 (OWNED_BY, HAS_EVENT 등).
- 한국어.
"""


def _retrieve(session: QASession, question: str) -> dict[str, Any]:
    """간단한 키워드 매칭 + 축 키워드 기반 retrieval."""
    if not session.kg:
        return {"empty": True}

    out: dict[str, Any] = {"graph_summary": session.kg.summary()}

    # 축 키워드 감지
    for axis in ("ESG", "AWARENESS", "VITALITY", "인지도", "상권", "환경"):
        if axis in question:
            mapping = {"인지도": "AWARENESS", "상권": "VITALITY", "환경": "ESG"}
            axis_key = mapping.get(axis, axis)
            out[f"axis_evidence_{axis_key}"] = session.kg.axis_evidence(axis_key, top_k=15)

    # 자산명 기반 이웃
    label = session.address_label()
    nbrs = session.kg.neighbors(label)
    if nbrs.get("found"):
        out["asset_neighbors"] = nbrs

    # 질문에서 명사구로 엔티티 검색 (단순: 띄어쓰기 토큰 중 한글 2자 이상)
    tokens = [t for t in question.split() if len(t) >= 2]
    found_entities: list[dict[str, Any]] = []
    for tok in tokens[:6]:
        hits = session.kg.search_entities(tok, top_k=5)
        if hits:
            found_entities.extend(hits)
    if found_entities:
        # 중복 제거
        seen: set[str] = set()
        unique = []
        for h in found_entities:
            if h["id"] not in seen:
                seen.add(h["id"])
                unique.append(h)
        out["entity_hits"] = unique[:15]

    return out


def run(question: str, session: QASession, callbacks: list[Any] | None = None) -> str:
    retrieval = _retrieve(session, question)
    llm = make_llm(max_tokens=1500)
    body = (
        f"# 대상 자산: {session.address_label()}\n\n"
        f"# 지식그래프 retrieval 결과\n```json\n{json.dumps(retrieval, ensure_ascii=False, default=str)[:24000]}\n```\n\n"
        f"# 대화 히스토리\n{session.history_excerpt() or '(없음)'}\n\n"
        f"# 질문\n{question}"
    )
    resp = llm.invoke(
        [SystemMessage(content=SYSTEM), HumanMessage(content=body)],
        config={"callbacks": callbacks or []},
    )
    return resp.content if isinstance(resp.content, str) else str(resp.content)
