"""Final report synthesizer (Claude Opus)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .. import config
from . import _llm

SYSTEM = """당신은 부동산 STO(증권형 토큰) 발행 자문 시 사회적 가치 평가 리포트를 작성하는 시니어 애널리스트다.
3개 축(ESG / 인지도 / 상권·입지 활성도)의 평가 결과와 원천 메타데이터를 받아 한국어 보고서를 작성한다.

작성 규칙:
- 분량은 충실히. 단, 사실 위주. 검증되지 않은 단정 금지.
- 결측·미신청 데이터는 '데이터 한계' 섹션에 명시.
- 점수가 N/A인 축은 가중치에서 제외하고 그 사실을 명시.
- 마크다운 사용. 표 적극 활용.

보고서 구조:
1. 표지/요약 (한 줄 종합 의견 + 종합 등급)
2. 대상 자산 개요 (주소, 식별자, 건물 메타, 토지 메타)
3. 평가 요약 표 (축별 점수/등급/한 줄 코멘트)
4. ESG 상세
5. 인지도 상세
6. 상권·입지 활성도 상세
7. 종합 STO 적합성 의견 (투자자 관점, 발행 시 강조포인트, 리스크 디스클로저)
8. 데이터 한계 및 출처
"""


def _composite(axes: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [a for a in axes if isinstance(a.get("score"), (int, float))]
    if not valid:
        return {"composite_score": None, "grade": "N/A"}
    avg = sum(a["score"] for a in valid) / len(valid)
    grade = (
        "A+" if avg >= 90
        else "A" if avg >= 80
        else "B+" if avg >= 70
        else "B" if avg >= 60
        else "C+" if avg >= 50
        else "C" if avg >= 40
        else "D"
    )
    return {"composite_score": round(avg, 1), "grade": grade}


def run(state: dict[str, Any], callbacks: list[Any] | None = None) -> dict[str, Any]:
    axes = [state.get("esg") or {}, state.get("awareness") or {}, state.get("vitality") or {}]
    composite = _composite(axes)

    payload = {
        "generated_at": date.today().isoformat(),
        "address_input": state.get("address"),
        "metadata": state.get("metadata"),
        "axes": {
            "esg": state.get("esg"),
            "awareness": state.get("awareness"),
            "vitality": state.get("vitality"),
        },
        "composite": composite,
    }

    llm = _llm.make_llm(model=config.REPORT_MODEL, max_tokens=8192, temperature=0.3)
    user_msg = (
        "아래 평가 결과 JSON을 기반으로 위 지시대로 보고서를 작성하라.\n"
        "출력은 마크다운 본문만 (코드펜스 없이).\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, default=str)[:120000]}\n```"
    )
    resp = llm.invoke(
        [SystemMessage(content=SYSTEM), HumanMessage(content=user_msg)],
        config={"callbacks": callbacks or []},
    )
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    return {"composite": composite, "report_markdown": text}
