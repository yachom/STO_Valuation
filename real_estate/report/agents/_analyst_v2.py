"""2-step axis analyst — 초기 retriever evidence → scout 호출 결정 → 통합 본문.

각 axis(tenant/esg/commercial)별로 동일 패턴.

Step 1 — Sonnet
    초기 evidence를 보고 "어느 scout를 어떤 인자로 호출할지" + 1차 draft를
    structured output으로 반환.

Step 2 — Python (no LLM)
    data_scout를 직접 호출해 보강 evidence 수집.

Step 3 — Sonnet
    1차 draft + 신규 scout 결과를 통합해 최종 섹션 본문 작성.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from real_estate.report.agents.base import format_snippets, korean_system_prompt
from real_estate.report.agents.data_scout import SCOUT_REGISTRY, DataScout
from real_estate.report.llm import ModelTier, build_llm
from real_estate.report.rag.protocol import Axis

logger = logging.getLogger("agent.analyst_v2")


class ScoutCall(BaseModel):
    method: str = Field(description=(
        "호출할 scout 함수 이름. 가능: " +
        ", ".join(f"{n}(axis={a})" for n, a, _ in SCOUT_REGISTRY)
    ))
    sigungu: str | None = Field(default=None, description="자치구명 (예: '강남구')")
    trdar_cd: str | None = Field(default=None, description="상권 코드 (commercial 전용)")
    canonical_name: str | None = Field(default=None, description="단지명 (self_certified 전용)")
    master_complex_id: int | None = Field(default=None)


class Step1Out(BaseModel):
    draft_markdown: str = Field(description="현 evidence만으로 1차 작성된 섹션 markdown")
    scout_calls: list[ScoutCall] = Field(
        default_factory=list,
        description="추가로 호출할 scout 함수 호출 명세 (최대 3개)",
    )
    confidence: float = Field(description="현재 draft 확신도 0~1")


class Step3Out(BaseModel):
    body_markdown: str = Field(description="최종 섹션 본문 markdown")
    used_evidence_indexes: list[int] = Field(default_factory=list)
    confidence: float = Field(description="최종 확신도 0~1")


def make_v2_analyst_node(axis: Axis, role_label: str, db_path: str):
    """v2 multi-step analyst 노드를 반환."""

    system_1 = korean_system_prompt(
        f"{role_label} 분석가 1단계. 주어진 초기 evidence로 1차 draft를 만들고, "
        f"부족하다 판단되면 SCOUT를 호출해 통계를 보강하라."
    )
    system_3 = korean_system_prompt(
        f"{role_label} 분석가 2단계 통합. 1차 draft에 scout 결과를 합쳐 "
        f"3~6단락 markdown으로 마무리. 모든 사실에 [E#] 또는 [S#] 인용."
    )

    available_scouts = [s for s in SCOUT_REGISTRY if s[1] == axis]
    scout_desc = "\n".join(f"- {name}: {desc}" for name, _, desc in available_scouts)

    def node(state: dict, ctx) -> dict:
        outline = state.get("section_outline", [])
        my = next((s for s in outline if s["axis"] == axis), None)
        if my is None:
            logger.warning("axis=%s 섹션이 outline에 없음", axis)
            return {}

        question = my["question"]
        snippets = ctx.retriever.retrieve(question, k=6, axis=axis)
        evidence_text = format_snippets(snippets)

        # ── Step 1: scout 결정 + 1차 draft ──
        step1_user = (
            f"이번 섹션: {my['title']}\n핵심 질문: {question}\n\n"
            f"=== 가용 evidence ===\n{evidence_text}\n\n"
            f"=== 가용 SCOUT 함수 (이 축) ===\n{scout_desc}\n\n"
            "위 evidence로 1차 draft를 작성하고, 더 필요한 통계가 있다면 scout 호출 명세를 "
            "최대 3개까지 명시하세요. 충분하면 빈 리스트."
        )
        llm1 = build_llm(ModelTier.SONNET).with_structured_output(Step1Out)
        step1: Step1Out = llm1.invoke([
            {"role": "system", "content": system_1},
            {"role": "user", "content": step1_user},
        ])
        logger.info("axis=%s step1 done — confidence=%.2f, scouts=%d",
                    axis, step1.confidence, len(step1.scout_calls))

        # ── Step 2: scout 실행 (Python only) ──
        scout = DataScout(db_path)
        scout_results: list[tuple[str, str]] = []  # (text, ref)
        for call in step1.scout_calls[:3]:
            try:
                method = getattr(scout, call.method, None)
                if method is None:
                    logger.warning("unknown scout: %s", call.method)
                    continue
                # 함수별 인자 매핑 — 모두 keyword
                kwargs = {}
                if call.sigungu is not None:
                    kwargs["sigungu"] = call.sigungu
                if call.trdar_cd is not None:
                    kwargs["trdar_cd"] = call.trdar_cd
                if call.canonical_name is not None:
                    kwargs["canonical_name"] = call.canonical_name
                if call.master_complex_id is not None:
                    kwargs["master_complex_id"] = call.master_complex_id
                result = method(**kwargs)
                scout_results.append((result.text, result.ref))
            except Exception as exc:
                logger.warning("scout %s failed: %s", call.method, exc)

        # ── Step 3: 통합 ──
        scout_text = (
            "\n".join(f"[S{i+1}] {t}" for i, (t, _) in enumerate(scout_results))
            if scout_results else "(추가 scout 결과 없음)"
        )
        step3_user = (
            f"섹션 제목: {my['title']}\n질문: {question}\n\n"
            f"=== 초기 evidence ===\n{evidence_text}\n\n"
            f"=== Scout 보강 결과 ===\n{scout_text}\n\n"
            f"=== 1차 draft (참고만 — 그대로 쓰지 말 것) ===\n{step1.draft_markdown}\n\n"
            "위 두 종류 출처를 통합해 3~6단락 markdown 본문을 완성하세요. "
            "초기 evidence는 [E#], scout 결과는 [S#]으로 표시. "
            "데이터 부족 영역은 솔직히 명시."
        )
        llm3 = build_llm(ModelTier.SONNET).with_structured_output(Step3Out)
        step3: Step3Out = llm3.invoke([
            {"role": "system", "content": system_3},
            {"role": "user", "content": step3_user},
        ])

        # ── 저장 ──
        evidence_refs: list[str] = []
        evidence_snippets: list[str] = []
        for i in step3.used_evidence_indexes:
            if 1 <= i <= len(snippets):
                evidence_refs.extend(snippets[i - 1].node_refs)
                evidence_snippets.append(snippets[i - 1].text)
        for text, ref in scout_results:
            evidence_refs.append(ref)
            evidence_snippets.append(text)

        sections = dict(state.get("sections", {}))
        sections[my["section_id"]] = {
            "axis": axis,
            "title": my["title"],
            "body_markdown": step3.body_markdown,
            "evidence_node_refs": list(dict.fromkeys(evidence_refs)),
            "evidence_snippets": evidence_snippets,
            "confidence_step1": step1.confidence,
            "confidence_final": step3.confidence,
            "scout_calls_made": [c.method for c in step1.scout_calls[:3]],
        }
        return {"sections": sections}

    return node
