"""CompositeRecalc: 가중치/임계값 변경 시뮬레이션.

질문에서 가중치를 추출해 종합 점수 재계산. LLM은 결과 해설용.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ...agents._llm import make_llm
from ..session import QASession


def _grade_for(score: float) -> str:
    return (
        "A+" if score >= 90
        else "A" if score >= 80
        else "B+" if score >= 70
        else "B" if score >= 60
        else "C+" if score >= 50
        else "C" if score >= 40
        else "D"
    )


def _parse_weights(question: str) -> dict[str, float] | None:
    """질문 내 'ESG 50%, 인지도 30%, 상권 20%' 같은 패턴 파싱."""
    pattern = re.compile(r"(ESG|인지도|awareness|상권|입지|vitality)\s*(?:을|를)?\s*(?:(\d+)\s*%|(0\.\d+))", re.IGNORECASE)
    found: dict[str, float] = {}
    name_map = {
        "esg": "esg", "인지도": "awareness", "awareness": "awareness",
        "상권": "vitality", "입지": "vitality", "vitality": "vitality",
    }
    for m in pattern.finditer(question):
        key = name_map.get(m.group(1).lower())
        if not key:
            continue
        if m.group(2):
            found[key] = float(m.group(2)) / 100.0
        elif m.group(3):
            found[key] = float(m.group(3))
    return found or None


def _recalc(session: QASession, weights: dict[str, float] | None) -> dict[str, Any]:
    axes = {k: session.axis(k) for k in ("esg", "awareness", "vitality")}
    scores = {k: v.get("score") for k, v in axes.items() if isinstance(v.get("score"), (int, float))}
    if not scores:
        return {"error": "no valid axis scores"}

    if weights:
        # 결측 채우기 + 정규화
        w = {k: weights.get(k, 0.0) for k in scores}
        s = sum(w.values())
        if s <= 0:
            w = {k: 1.0 / len(scores) for k in scores}
        else:
            w = {k: v / s for k, v in w.items()}
    else:
        w = {k: 1.0 / len(scores) for k in scores}

    composite = sum(scores[k] * w[k] for k in scores)
    return {
        "weights": w,
        "axis_scores": scores,
        "composite_score": round(composite, 1),
        "grade": _grade_for(composite),
        "original_composite": session.composite(),
    }


SYSTEM = """당신은 부동산 STO 평가의 종합 점수 시뮬레이션 해설자다.
주어진 재계산 결과를 사용자에게 표·요약으로 설명하라. 한국어. 추측 금지."""


def run(question: str, session: QASession, callbacks: list[Any] | None = None) -> str:
    weights = _parse_weights(question)
    recalc = _recalc(session, weights)

    llm = make_llm(max_tokens=900)
    body = (
        f"# 재계산 결과\n```json\n{json.dumps(recalc, ensure_ascii=False)}\n```\n\n"
        f"# 질문\n{question}\n\n"
        "가중치 입력 여부, 기존 대비 변화, 등급 변화를 명확히 설명하라."
    )
    resp = llm.invoke(
        [SystemMessage(content=SYSTEM), HumanMessage(content=body)],
        config={"callbacks": callbacks or []},
    )
    return resp.content if isinstance(resp.content, str) else str(resp.content)
