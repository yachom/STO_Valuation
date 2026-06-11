"""Prefetch — identifier 가용성만으로 결정되는 baseline scout 묶음 실행.

식별자(bd_mgt_sn / 좌표 / sigungu_cd / adstrd_cd)가 정해지는 순간 결과가 고정되는
도구들을 한번에 묶어 호출. 호출자(스크립트 or 그래프 노드)가 planner_v3 호출 전에
미리 돌려 evidence_pool을 만들어 state에 주입하는 게 권장.

planner_v3는 state.evidence_pool이 비어 있으면 자동 폴백으로 이 함수를 호출하므로,
graph_v2에 drop-in해도 동작 (legacy 호환).

PREFETCH_PLAN 정의:
    (scout_method, axis, {scout_kwarg: identifier_key}, fixed_kwargs)
    - scout_kwarg: DataScout 메소드 시그니처상 인자 이름
    - identifier_key: asset_identifiers dict의 key
    - fixed_kwargs: 식별자 무관 고정 인자 (region_level 등)
"""

from __future__ import annotations

import logging
import uuid

from real_estate.report.tool.scout.data_scout import DataScout
from real_estate.report.tracing import emit_tool_call

logger = logging.getLogger("prefetch")


PREFETCH_PLAN: list[tuple[str, str, dict, dict]] = [
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


def run_prefetch(
    identifiers: dict, db_path: str,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """식별자가 충분한 도구만 호출.

    Returns
    -------
    legacy_facts : dict[method_name, {text, ref, axis}]
        graph_v2 호환용 — 기존 planner.py가 반환하던 prefetched_facts 형태.
    evidence_pool : dict[ev_id, {ev_id, text, ref, source, axis}]
        planner_v3 mind_map 시드용.

    각 scout 호출은 tracing.emit_tool_call로 기록 (status: ok/skip_missing_id/error/unknown).
    실패한 scout는 건너뛰고 계속 — 부분 결과 반환.
    """
    facts: dict[str, dict] = {}
    evidence_pool: dict[str, dict] = {}
    if not identifiers:
        return facts, evidence_pool

    scout = DataScout(db_path)
    for method_name, axis, kwarg_to_id, fixed_kwargs in PREFETCH_PLAN:
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
            ev_id = f"ev_{uuid.uuid4().hex[:8]}"
            evidence_pool[ev_id] = {
                "ev_id": ev_id,
                "text": result.text,
                "ref": result.ref,
                "source": f"prefetch:{method_name}",
                "axis": axis,
            }
            emit_tool_call(
                tool=f"scout:{method_name}", status="ok",
                args=kwargs, result={"text": result.text, "ref": result.ref},
            )
        except Exception as exc:
            logger.warning("prefetch %s failed: %s", method_name, exc)
            emit_tool_call(
                tool=f"scout:{method_name}", status="error",
                args=kwargs, result={"error": str(exc)[:300]},
            )
    return facts, evidence_pool
