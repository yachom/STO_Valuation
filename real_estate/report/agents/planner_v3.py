"""Planner V3 — Co-STORM 기반 mind map 관리자. (Phase A: initial 모드만)

Co-STORM(Jiang et al., EMNLP 2024)의 moderator + mind map insert 메커니즘을
한국 상가 부동산 평가용으로 어댑트. Phase A는 initial 호출 1회만 다루며 향후
Phase B에서 replan 모드(critic fail → 새 focus_question 주입)를 추가한다.

설계 원칙:
    - 사전 정보 확장(prefetch)은 외부에서 별도 단계로 실행 후 state.evidence_pool로 전달.
      [[feedback_planner_what_how]] — planner는 도구 선택을 직접 안 함.
    - state.evidence_pool이 비어 있으면 자동 폴백으로 prefetch 호출 (graph_v2 drop-in 유지).

initial 모드 입력 (state):
    asset_summary, asset_identifiers (필수)
    evidence_pool         (권장 — 외부 prefetcher가 채워둠. 없으면 자동 폴백)
    prefetched_facts      (옵션 — graph_v2 호환)
    prior_human_comments  (옵션 — 같은 자산 직전 라운드 누적 Review 코멘트)
    prior_qa_gaps         (옵션 — 같은 자산 직전 라운드 QA 'out-of-scope' 질문들)

initial 모드 출력 (state delta):
    mind_map        — Co-STORM 식 hierarchical 트리 dict
    evidence_pool   — 입력 그대로 또는 fallback으로 새로 채움
    discourse_log   — turn 1 기록
    section_outline — graph_v2 호환 (분석가가 그대로 소비 가능)
    prefetched_facts — graph_v2 호환
    replan_iter = 0

호출:
    legacy 그래프와 dual-track. graph_v2의 planner 자리에 swap 가능 (drop-in).
    단독 시험은 scripts/try_planner_v3.py.
"""

from __future__ import annotations

import logging
import uuid

from pydantic import BaseModel, Field

from real_estate.report.agents.base import korean_system_prompt
from real_estate.report.llm import cached_system, get_llm
from real_estate.report.tool.mindmap import MindMap
from real_estate.report.tool.scout.prefetch import run_prefetch
from real_estate.report.tracing import emit, node_scope

logger = logging.getLogger("agent.planner_v3")


# ─── LLM structured outputs ────────────────────────────────

class FocusQuestion(BaseModel):
    axis: str = Field(description="tenant / esg / commercial 중 하나")
    title: str = Field(description="섹션 제목 (15자 이내)")
    question: str = Field(description="해당 축의 핵심 focus question")


class InitialFocusOutput(BaseModel):
    """3축 root concept에 부여할 focus question."""
    focus: list[FocusQuestion] = Field(
        description="정확히 3개 — tenant/esg/commercial 각 1개씩",
    )


class SignalConcept(BaseModel):
    """직전 라운드 human comment 또는 QA gap을 흡수한 새 sub-concept."""
    source_index: int = Field(description="입력 signal 리스트의 0-base 인덱스")
    axis: str = Field(description="tenant / esg / commercial 중 가장 가까운 축")
    label: str = Field(description="새 sub-concept 라벨 (10자 이내, 평가 도메인 용어)")
    focus_question: str = Field(description="이 signal을 다음 보고서에서 다룰 구체 질문")


class SignalAbsorptionOutput(BaseModel):
    concepts: list[SignalConcept] = Field(
        description="입력 signal 개수만큼. signal 인덱스를 source_index에 정확히 표기.",
    )


class ReplanConcept(BaseModel):
    """critic + reflection을 반영해 추가할 신규 sub-concept."""
    axis: str = Field(description="tenant / esg / commercial")
    label: str = Field(description="새 sub-concept 라벨 (10자 이내, 평가 도메인 용어)")
    focus_question: str = Field(
        description="다음 라운드 analyst가 retrieve+scout로 답할 구체 질문",
    )
    updated_axis_question: str | None = Field(
        default=None,
        description=("이 sub-concept이 axis 핵심을 바꿀 만큼 중요하면 axis root의 "
                     "focus_question 갱신 버전을 적어 analyst가 그것을 새 질문으로 사용. "
                     "불필요하면 null."),
    )


class ReplanOutput(BaseModel):
    new_concepts: list[ReplanConcept] = Field(
        description=("0~3개. critic의 본질이 정보 공백이면 그 공백을 메울 sub-concept. "
                     "단순 어조·구조 문제면 빈 리스트 (final_editor가 처리)."),
    )


# ─── 시스템 프롬프트 ───────────────────────────────────────

_FOCUS_SYSTEM = korean_system_prompt(
    "보고서 기획자. 자산 요약 + 사전 수집된 베이스라인 사실 + 직전 라운드 사람 코멘트/QA "
    "공백을 통합하여 3축(tenant/esg/commercial)별 핵심 focus question을 생성한다. "
    "사실이 답을 가리키면 더 좁히는 질문을, 공백이 있으면 메우는 질문을 만든다."
)

_ABSORB_SYSTEM = korean_system_prompt(
    "보고서 기획자. 직전 라운드의 사람 Review 코멘트와 QA 'out-of-scope' 누적을 받아 "
    "각 signal이 어느 축(tenant/esg/commercial)에 속하는지 분류하고, "
    "다음 보고서의 새 sub-concept으로 변환한다. overview처럼 축에 속하지 않는 코멘트는 "
    "가장 가까운 축으로 매핑."
)

_REPLAN_SYSTEM = korean_system_prompt(
    "보고서 moderator (Co-STORM 식). 직전 라운드 본문이 critic의 비판을 받았다. "
    "비판의 본질이 *정보 측면의 공백*이면 mind_map에 새 sub-concept으로 변환해 "
    "다음 analyst가 무엇을 더 파야 하는지 명시한다. 단순 어조·중복은 무시 (final_editor가 처리). "
    "공백이 명확하지 않으면 new_concepts=[]."
)


# ─── 포맷 헬퍼 ───────────────────────────────────────────────

def _format_axis_block(mind_map: MindMap, evidence_pool: dict, axis: str,
                       label: str) -> str:
    """LLM 입력용 — axis root 아래 evidence + 흡수된 sub-concept hint 한 블록."""
    axis_concept = mind_map.concepts.get(axis)
    if not axis_concept:
        return f"<{label}>\n(데이터 없음)"

    ev_lines = []
    for ev_id in axis_concept.evidence_ids:
        ev = evidence_pool.get(ev_id)
        if ev:
            ev_lines.append(f"- {ev['text']}")
    ev_block = "\n".join(ev_lines) if ev_lines else "(prefetch 결과 없음)"

    sub_lines = []
    for child_id in axis_concept.children:
        child = mind_map.concepts.get(child_id)
        if not child:
            continue
        sub_lines.append(
            f"  · [{child.origin}] {child.label}: {child.focus_question}"
        )
    sub_block = ""
    if sub_lines:
        sub_block = "\n(직전 라운드 누적 sub-concept 요구:\n" + "\n".join(sub_lines) + ")"

    return f"<{label}>\n{ev_block}{sub_block}"


def _flatten_signals(prior_human_comments: list[dict],
                     prior_qa_gaps: list[dict]) -> tuple[str, list[dict]]:
    """human_comments + qa_gaps 평탄화 + LLM 입력 텍스트 생성."""
    flat: list[dict] = []
    for c in prior_human_comments or []:
        flat.append({
            "kind": "human_comment",
            "instruction": c.get("instruction") or c.get("message") or "",
            "intent": c.get("intent", ""),
            "section_axis": c.get("axis"),
        })
    for q in prior_qa_gaps or []:
        flat.append({
            "kind": "qa_out_of_scope",
            "instruction": q.get("question", ""),
            "intent": "qa_missing",
            "section_axis": None,
        })

    if not flat:
        return "(신호 없음)", flat

    lines = []
    for i, s in enumerate(flat):
        hint = f" (섹션 axis 힌트: {s['section_axis']})" if s["section_axis"] else ""
        lines.append(
            f"[{i}] kind={s['kind']} intent={s['intent']}{hint}\n"
            f"    내용: {s['instruction']}"
        )
    return "\n".join(lines), flat


# ─── replan 모드 helper ─────────────────────────────────────

def _run_replan_mode(state: dict) -> dict:
    """critic_output + reflection_memo + 현 mind_map → 새 sub-concept(s).

    LLM 1회 (Opus). new_concepts가 비면 mind_map 변경 없이 replan_iter만 증가.
    """
    with node_scope("planner_v3", mode="replan",
                    input_keys=list(state.keys())):
        # state 복원
        existing_mind_map = state.get("mind_map") or {}
        if not existing_mind_map:
            # 방어 — 이론상 dispatcher가 막아주지만
            logger.warning("replan mode entered without mind_map; treating as no-op")
            return {"replan_iter": (state.get("replan_iter", 0)) + 1}

        mind_map = MindMap.from_dict(existing_mind_map)
        evidence_pool = state.get("evidence_pool") or {}
        existing_outline = list(state.get("section_outline") or [])
        sections = state.get("sections") or {}
        critic_output = state.get("critic_output") or {}
        reflection_memo = state.get("reflection_memo") or ""
        current_iter = state.get("replan_iter", 0)

        # LLM 입력 텍스트 준비
        mind_map_summary = mind_map.pretty(
            evidence_pool=evidence_pool, max_ev_show=1,
        )
        all_issues = critic_output.get("issues") or []
        focused_issues = [
            it for it in all_issues
            if (it.get("severity") or "").lower() in ("high", "medium")
        ]
        issues_text = "\n".join(
            f"- [{i.get('severity')}/{i.get('kind')}] {i.get('location')}: "
            f"{i.get('description')} → {i.get('suggestion')}"
            for i in focused_issues[:6]
        ) or "(high/medium issue 없음)"
        sections_summary = "\n".join(
            f"- [{s.get('axis')}] {s.get('title','')} "
            f"(conf={s.get('confidence_final','?')})"
            for s in sections.values()
        ) or "(섹션 없음)"

        user = (
            f"=== 현재 mind map ===\n{mind_map_summary}\n\n"
            f"=== 직전 라운드 섹션 요약 ===\n{sections_summary}\n\n"
            f"=== critic 지적 (high/medium) ===\n{issues_text}\n\n"
            f"=== reflection memo ===\n{reflection_memo or '(없음)'}\n\n"
            "위 비판의 본질이 *정보 공백*이면, 그 공백을 메울 sub-concept을 0~3개 제안하세요. "
            "axis는 tenant/esg/commercial 중 하나. label은 10자 이내 도메인 용어. "
            "focus_question은 analyst가 retrieve+scout로 답할 구체 질문. "
            "단순 어조·구조 문제로만 보이면 new_concepts=[]."
        )
        llm = get_llm("planner_v3").with_structured_output(ReplanOutput)
        try:
            out: ReplanOutput = llm.invoke([
                {"role": "system", "content": cached_system(_REPLAN_SYSTEM)},
                {"role": "user", "content": user},
            ])
        except Exception as exc:
            logger.warning("replan LLM failed: %s — incrementing iter only", exc)
            out = ReplanOutput(new_concepts=[])

        # 적용
        new_concepts_added: list[dict] = []
        axis_question_updates: dict[str, str] = {}
        for nc in out.new_concepts:
            if nc.axis not in ("tenant", "esg", "commercial"):
                logger.warning("replan: unknown axis %s", nc.axis)
                continue
            cid = mind_map.add_sub_concept(
                parent_id=nc.axis,
                label=nc.label,
                focus_question=nc.focus_question,
                origin=f"replan_{current_iter + 1}",
                axis=nc.axis,  # type: ignore[arg-type]
            )
            new_concepts_added.append({
                "concept_id": cid, "axis": nc.axis, "label": nc.label,
            })
            if nc.updated_axis_question:
                axis_question_updates[nc.axis] = nc.updated_axis_question
                mind_map.set_focus_question(nc.axis, nc.updated_axis_question)

        # outline의 axis question 업데이트 (section_id 보존)
        new_outline: list[dict] = []
        for sec in existing_outline:
            ax = sec.get("axis")
            new_sec = dict(sec)
            if ax in axis_question_updates:
                new_sec["question"] = axis_question_updates[ax]
            new_outline.append(new_sec)

        log_entry = {
            "turn": current_iter + 2,  # initial=1, replan_iter 1→turn 2, ...
            "role": "planner_v3",
            "mode": "replan",
            "replan_iter": current_iter + 1,
            "new_concepts": [
                {"axis": c["axis"], "label": c["label"]}
                for c in new_concepts_added
            ],
            "axis_question_updates": list(axis_question_updates.keys()),
        }

        logger.info(
            "planner_v3 replan iter=%d — new_concepts=%d, axis_updates=%d",
            current_iter + 1, len(new_concepts_added), len(axis_question_updates),
        )
        emit("planner_v3_replan",
             iter=current_iter + 1,
             new_concepts=len(new_concepts_added),
             axis_updates=list(axis_question_updates.keys()))

        return {
            "mind_map": mind_map.to_dict(),
            "section_outline": new_outline,
            "discourse_log": [log_entry],
            "replan_iter": current_iter + 1,
        }


# ─── 메인 노드 팩토리 ────────────────────────────────────────

def make_planner_v3_node(db_path: str):
    """db_path closure로 잡은 planner_v3 노드.

    mode dispatch:
        state.mind_map 없으면 → initial (prefetch + 3축 시딩 + focus question)
        state.mind_map 있으면 → replan (critic+reflection → 새 sub-concept)
    """

    def node(state: dict) -> dict:
        # mode dispatcher
        if state.get("mind_map"):
            return _run_replan_mode(state)

        # ── initial 모드 (기존 로직) ──
        with node_scope("planner_v3", mode="initial",
                        input_keys=list(state.keys())):
            identifiers = state.get("asset_identifiers", {}) or {}
            asset_summary = state.get("asset_summary", "")
            prior_comments = state.get("prior_human_comments", []) or []
            prior_qa = state.get("prior_qa_gaps", []) or []

            # 1) evidence_pool — state에 있으면 사용, 없으면 폴백으로 prefetch
            #    (graph_v2 drop-in 호환 + 단독 시험 시 외부 prefetch 단계 인정)
            evidence_pool = dict(state.get("evidence_pool") or {})
            legacy_facts = dict(state.get("prefetched_facts") or {})
            if not evidence_pool:
                legacy_facts, evidence_pool = run_prefetch(identifiers, db_path)
                emit("planner_prefetch", count=len(evidence_pool),
                     methods=list(legacy_facts.keys()),
                     source="fallback")
            else:
                emit("planner_prefetch", count=len(evidence_pool),
                     methods=list(legacy_facts.keys()),
                     source="state")

            # 2) mind map seeding — axis root에 evidence 분류
            mind_map = MindMap()
            for ev_id, ev in evidence_pool.items():
                axis = ev["axis"]
                if axis in mind_map.concepts:
                    mind_map.add_evidence_to(axis, ev_id)

            # 3) prior signals 흡수 (있을 때만)
            signal_concepts_added: list[dict] = []
            if prior_comments or prior_qa:
                signal_text, flat_signals = _flatten_signals(
                    prior_comments, prior_qa,
                )
                absorb_user = (
                    f"자산 요약:\n{asset_summary}\n\n"
                    f"=== 직전 라운드 누적 신호 ===\n{signal_text}\n\n"
                    "각 signal을 가장 가까운 axis(tenant/esg/commercial)로 분류하고, "
                    "다음 보고서의 sub-concept으로 변환하세요. "
                    "signal 개수만큼 SignalConcept을 출력하고, source_index는 입력 signal의 "
                    "0-base 인덱스를 정확히 사용하세요."
                )
                llm_absorb = get_llm("planner_v3").with_structured_output(
                    SignalAbsorptionOutput,
                )
                try:
                    absorbed: SignalAbsorptionOutput = llm_absorb.invoke([
                        {"role": "system", "content": cached_system(_ABSORB_SYSTEM)},
                        {"role": "user", "content": absorb_user},
                    ])
                except Exception as exc:
                    logger.warning("signal absorb failed: %s — skipping", exc)
                    absorbed = SignalAbsorptionOutput(concepts=[])

                for sc in absorbed.concepts:
                    if sc.axis not in ("tenant", "esg", "commercial"):
                        logger.warning("absorb: unknown axis %s for signal %d",
                                       sc.axis, sc.source_index)
                        continue
                    if not (0 <= sc.source_index < len(flat_signals)):
                        logger.warning("absorb: source_index %d out of range",
                                       sc.source_index)
                        continue
                    src = flat_signals[sc.source_index]
                    origin = "hitl" if src["kind"] == "human_comment" else "qa"
                    cid = mind_map.add_sub_concept(
                        parent_id=sc.axis,
                        label=sc.label,
                        focus_question=sc.focus_question,
                        origin=origin,
                        axis=sc.axis,  # type: ignore[arg-type]
                    )
                    signal_concepts_added.append({
                        "concept_id": cid, "axis": sc.axis, "label": sc.label,
                        "origin": origin, "source_index": sc.source_index,
                    })
                emit("planner_signal_absorption",
                     signals_in=len(flat_signals),
                     concepts_out=len(signal_concepts_added))

            # 4) 3축 focus question 생성 (Opus 1회)
            blocks = [
                _format_axis_block(mind_map, evidence_pool, "tenant", "테넌트"),
                _format_axis_block(mind_map, evidence_pool, "esg", "ESG"),
                _format_axis_block(mind_map, evidence_pool, "commercial", "상권"),
            ]
            focus_user = (
                f"자산 요약:\n{asset_summary}\n\n"
                f"=== axis별 베이스라인 사실 + (있다면) 누적 신호 ===\n"
                + "\n\n".join(blocks)
                + "\n\n각 축에 대해 정확히 1개씩 focus question을 만드세요. "
                  "베이스라인 사실이 답을 가리키면 더 좁히는 질문, 누적 sub-concept이 있으면 "
                  "그것을 출발점으로 메우는 질문, 둘 다 약하면 가용 데이터로 검증 가능한 가설을 "
                  "묻는 질문."
            )
            llm_focus = get_llm("planner_v3").with_structured_output(
                InitialFocusOutput,
            )
            focus_out: InitialFocusOutput = llm_focus.invoke([
                {"role": "system", "content": cached_system(_FOCUS_SYSTEM)},
                {"role": "user", "content": focus_user},
            ])

            # 5) mind_map 각 axis root에 focus_question 부여 + legacy outline 생성
            outline: list[dict] = []
            for fq in focus_out.focus:
                if fq.axis not in ("tenant", "esg", "commercial"):
                    logger.warning("focus output unknown axis: %s", fq.axis)
                    continue
                mind_map.set_focus_question(fq.axis, fq.question)
                outline.append({
                    "section_id": f"sec_{uuid.uuid4().hex[:8]}",
                    "axis": fq.axis,
                    "title": fq.title,
                    "question": fq.question,
                })

            # 6) discourse_log
            log_entry = {
                "turn": 1,
                "role": "planner_v3",
                "mode": "initial",
                "evidence_count": len(evidence_pool),
                "signal_concepts_added": len(signal_concepts_added),
                "axis_focus_questions": {fq.axis: fq.question for fq in focus_out.focus},
            }

            logger.info(
                "planner_v3 initial — facts=%d, signals_absorbed=%d, outline=%d",
                len(evidence_pool), len(signal_concepts_added), len(outline),
            )
            emit("planner_v3_initial",
                 evidence_count=len(evidence_pool),
                 mind_map_concepts=len(mind_map.concepts),
                 signals_absorbed=len(signal_concepts_added),
                 outline_sections=len(outline))

            return {
                "mind_map": mind_map.to_dict(),
                "evidence_pool": evidence_pool,
                "discourse_log": [log_entry],
                "section_outline": outline,
                "prefetched_facts": legacy_facts,
                "replan_iter": 0,
            }

    return node
