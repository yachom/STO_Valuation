"""Planner — asset_summary + prefetch facts를 받아 3축 섹션 outline 생성. (Opus 등급)

플래너는 outline을 만들기 전에 "이 건물·이 좌표·이 자치구·이 행정동이
정해지는 순간 결과가 고정되는" 도구들(`_PREFETCH_PLAN`)을 무조건 호출해
컨텍스트를 풍부화한 뒤 계획한다. 분석가는 같은 facts를 state로 받아 자기
축의 추가 scout 결정에 참고한다. 도구 자체는 `tool/scout/data_scout.py`.
"""

from __future__ import annotations

import logging
import uuid

from pydantic import BaseModel, Field

from real_estate.report.agents.base import korean_system_prompt
from real_estate.report.tool.scout.data_scout import DataScout
from real_estate.report.llm import cached_system, get_llm
from real_estate.report.tracing import emit, emit_tool_call, node_scope

logger = logging.getLogger("agent.planner")


class SectionPlan(BaseModel):
    axis: str = Field(description="tenant / esg / commercial 중 하나")
    title: str = Field(description="섹션 제목 (15자 이내)")
    question: str = Field(description="해당 축에 대해 본 자산을 평가하기 위한 핵심 질문")


class PlannerOutput(BaseModel):
    sections: list[SectionPlan] = Field(
        description="정확히 3개의 섹션 — tenant, esg, commercial 각 1개씩."
    )


_SYSTEM = korean_system_prompt(
    "보고서 기획자. 자산 요약과 사전 수집된 베이스라인 사실을 받아 "
    "3축(테넌트/ESG/상권)에 대해 평가 섹션을 각 1개씩 기획하고, "
    "각 섹션의 핵심 질문을 만든다. 사실이 이미 답을 가리키면 그것을 "
    "더 좁히는 질문을, 사실이 비어 있으면 그 공백을 메우는 질문을 만든다."
)


# (scout_method, axis, {scout_kwarg: identifier_key}, fixed_kwargs)
# - scout_kwarg는 DataScout 메소드 시그니처상 인자 이름.
# - identifier_key는 asset_identifiers dict의 key (이름이 다른 경우 매핑).
# - fixed_kwargs는 식별자와 무관하게 항상 같이 보내는 인자 (region_level 등).
_PREFETCH_PLAN: list[tuple[str, str, dict, dict]] = [
    # ESG
    ("building_self_cert", "esg", {"bd_mgt_sn": "bd_mgt_sn"}, {}),
    ("cert_grade_distribution", "esg", {"sigungu": "sigungu"}, {}),
    ("gseed_grade_distribution", "esg", {"sigungu": "sigungu"}, {}),
    # Tenant
    ("lease_market_summary", "tenant", {"sigungu_cd": "sigungu_cd"}, {}),
    ("commercial_sale_stats_in_district", "tenant",
     {"sigungu_cd": "sigungu_cd"}, {}),
    ("business_churn_rate", "tenant",
     {"region_cd": "sigungu_cd"}, {"region_level": "sigungu"}),
    # Commercial
    ("transit_accessibility", "commercial",
     {"latitude": "latitude", "longitude": "longitude"}, {}),
    ("residential_demand", "commercial",
     {"latitude": "latitude", "longitude": "longitude"}, {}),
    ("development_pipeline", "commercial", {"sigungu": "sigungu"}, {}),
    ("admin_dong_demographics", "commercial",
     {"adstrd_cd": "adstrd_cd"}, {}),
    ("admin_dong_floating", "commercial", {"adstrd_cd": "adstrd_cd"}, {}),
    ("industry_top_near_coords", "commercial",
     {"latitude": "latitude", "longitude": "longitude"}, {}),
]


def _prefetch_facts(identifiers: dict, db_path: str) -> dict:
    """식별자가 충분한 도구만 호출. ScoutResult를 {name: {text, ref, axis}}로 변환."""
    facts: dict[str, dict] = {}
    if not identifiers:
        return facts

    scout = DataScout(db_path)
    for method_name, axis, kwarg_to_id, fixed_kwargs in _PREFETCH_PLAN:
        kwargs = {
            scout_kwarg: identifiers.get(id_key)
            for scout_kwarg, id_key in kwarg_to_id.items()
        }
        if any(v in (None, "") for v in kwargs.values()):
            emit_tool_call(
                tool=f"scout:{method_name}", status="skip_missing_id",
                args={"required": list(kwarg_to_id.values()),
                      "provided": {k: identifiers.get(k)
                                   for k in kwarg_to_id.values()}},
            )
            continue
        kwargs.update(fixed_kwargs)
        method = getattr(scout, method_name, None)
        if method is None:
            emit_tool_call(tool=f"scout:{method_name}", status="unknown")
            continue
        try:
            result = method(**kwargs)
            facts[method_name] = {
                "text": result.text, "ref": result.ref, "axis": axis,
            }
            emit_tool_call(
                tool=f"scout:{method_name}", status="ok",
                args=kwargs,
                result={"text": result.text, "ref": result.ref},
            )
        except Exception as exc:
            logger.warning("prefetch %s failed: %s", method_name, exc)
            emit_tool_call(
                tool=f"scout:{method_name}", status="error",
                args=kwargs, result={"error": str(exc)[:300]},
            )
    return facts


def _format_facts(facts: dict) -> str:
    if not facts:
        return ""
    by_axis: dict[str, list[str]] = {"tenant": [], "esg": [], "commercial": []}
    for name, payload in facts.items():
        axis = payload.get("axis", "?")
        by_axis.setdefault(axis, []).append(f"- [{name}] {payload['text']}")
    blocks: list[str] = []
    label = {"tenant": "테넌트", "esg": "ESG", "commercial": "상권"}
    for axis in ("tenant", "esg", "commercial"):
        lines = by_axis.get(axis) or []
        if not lines:
            continue
        blocks.append(f"<{label[axis]}>\n" + "\n".join(lines))
    return (
        "\n=== 사전 수집된 베이스라인 사실 (이 건물·좌표·자치구로부터 무조건 도출) ===\n"
        + "\n\n".join(blocks)
    )


def make_planner_node(db_path: str):
    """db_path를 closure로 잡은 planner 노드 반환."""

    def node(state: dict) -> dict:
        with node_scope("planner", input_keys=list(state.keys())):
            identifiers = state.get("asset_identifiers", {}) or {}
            facts = _prefetch_facts(identifiers, db_path)
            emit("planner_prefetch", count=len(facts),
                 methods=list(facts.keys()))

            llm = get_llm("planner").with_structured_output(PlannerOutput)
            asset_summary = state.get("asset_summary", "")
            user = (
                f"자산 요약:\n{asset_summary}\n"
                + _format_facts(facts)
                + "\n\n위 자산을 평가하기 위해 테넌트 가치 / ESG 가치 / 상권 가치 각 "
                "축마다 섹션 1개씩 기획해 주세요. 각 섹션의 핵심 질문은 위 "
                "베이스라인 사실을 출발점으로, 더 좁히거나 공백을 메울 구체적 "
                "질문으로 표현해 주세요."
            )
            result: PlannerOutput = llm.invoke([
                {"role": "system", "content": cached_system(_SYSTEM)},
                {"role": "user", "content": user},
            ])

            outline = []
            for sp in result.sections:
                outline.append({
                    "section_id": f"sec_{uuid.uuid4().hex[:8]}",
                    "axis": sp.axis,
                    "title": sp.title,
                    "question": sp.question,
                })
            logger.info(
                "planner produced %d sections (facts=%d)",
                len(outline), len(facts),
            )
            emit("planner_outline", sections=len(outline),
                 outline=[{"axis": o["axis"], "title": o["title"],
                           "question": o["question"]} for o in outline])
            return {
                "section_outline": outline,
                "prefetched_facts": facts,
            }

    return node
