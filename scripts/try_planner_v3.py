"""주소 → 사전 정보 확장 → planner_v3 동작 (Phase A).

사용자 입력은 주소 1개. 시스템이:
    [1] 주소를 식별자(좌표·bd_mgt_sn·sigungu_cd·adstrd_cd)로 확장
    [2] 식별자로 baseline scout 11종을 자동 호출 (evidence_pool 구성)
    [3] (Phase D 예정) 같은 자산의 직전 라운드 사람 코멘트/QA gap 자동 조회 — 지금은 빈 리스트
    [4] planner_v3 호출 → mind_map + focus questions 생성

전제:
    - real_estate.db 적재 완료
    - .env: ANTHROPIC_API_KEY, KAKAO_API_KEY, JUSO_API_KEY

사용:
    python -m scripts.try_planner_v3 "서울특별시 강남구 테헤란로 152"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from infra.env import load_env
from real_estate.location import LocationResolver
from real_estate.report.agents.planner_v3 import make_planner_v3_node
from real_estate.report.tool.scout.prefetch import run_prefetch

DB_PATH = Path("real_estate/database/DB/real_estate.db")


def _pretty_mind_map(mind_map_dict: dict, evidence_pool: dict,
                     max_ev_show: int = 3) -> str:
    lines: list[str] = []

    def _walk(cid: str, depth: int) -> None:
        c = mind_map_dict.get(cid)
        if c is None:
            return
        indent = "  " * depth
        ev_ids = c.get("evidence_ids") or []
        tag = f" [{len(ev_ids)} ev]" if ev_ids else ""
        axis = c.get("axis") or "-"
        origin = c.get("origin", "?")
        lines.append(f"{indent}- {c.get('label', cid)}{tag} (axis={axis}, origin={origin})")
        fq = c.get("focus_question")
        if fq:
            lines.append(f"{indent}    Q: {fq}")
        for ev_id in ev_ids[:max_ev_show]:
            ev = evidence_pool.get(ev_id, {})
            text = (ev.get("text") or "")[:90]
            lines.append(f"{indent}    · [{ev_id}] {text}")
        if len(ev_ids) > max_ev_show:
            lines.append(f"{indent}    · ... ({len(ev_ids) - max_ev_show} more)")
        for child in c.get("children") or []:
            _walk(child, depth + 1)

    _walk("root", 0)
    return "\n".join(lines)


def _header(label: str) -> None:
    print("\n" + "=" * 70)
    print(f"  {label}")
    print("=" * 70)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()

    missing = [k for k in (
        "ANTHROPIC_API_KEY", "KAKAO_API_KEY", "JUSO_API_KEY",
    ) if not os.environ.get(k)]
    if missing:
        sys.exit(f"env 누락: {', '.join(missing)}")

    parser = argparse.ArgumentParser()
    parser.add_argument("address", nargs="+",
                        help="상가 건물 주소 (자유 형식)")
    args = parser.parse_args()
    address = " ".join(args.address).strip()

    # ───────── Stage 1. 사용자 입력 ─────────
    _header("[1] 사용자 입력")
    print(f"주소: {address}")

    # ───────── Stage 2. 사전 정보 확장 ─────────
    _header("[2] 사전 정보 확장")

    # 2a. 주소 → 식별자
    print("\n2a. 주소 → 식별자 (LocationResolver: Kakao + Juso)")
    resolver = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"],
    )
    location = resolver.resolve(address)
    bcode = getattr(location, "bcode", None) or ""
    hcode = getattr(location, "hcode", None) or ""
    asset_identifiers = {
        "bd_mgt_sn": getattr(location, "bd_mgt_sn", None),
        "latitude": getattr(location, "latitude", None),
        "longitude": getattr(location, "longitude", None),
        "sigungu": getattr(location, "sigungu", None),
        "sigungu_cd": bcode[:5] if bcode else None,
        "adstrd_cd": hcode[:8] if hcode else None,
    }
    asset_summary = (
        f"주소: {address}\n"
        f"도로명: {getattr(location, 'road_address', '')}\n"
        f"지번: {getattr(location, 'jibun_address', '')}\n"
        f"건물관리번호: {getattr(location, 'bd_mgt_sn', '')}\n"
        f"좌표: ({getattr(location, 'latitude', '')}, "
        f"{getattr(location, 'longitude', '')})\n"
        f"시군구: {getattr(location, 'sigungu', '')}"
    )
    print(json.dumps(asset_identifiers, ensure_ascii=False, indent=2))

    # 2b. 식별자 → prefetched evidence_pool
    print("\n2b. 식별자 → baseline scout 자동 실행 (DataScout 11종)")
    legacy_facts, evidence_pool = run_prefetch(asset_identifiers, str(DB_PATH))
    by_axis: dict[str, int] = {"tenant": 0, "esg": 0, "commercial": 0}
    for ev in evidence_pool.values():
        by_axis[ev["axis"]] = by_axis.get(ev["axis"], 0) + 1
    print(f"  성공 {len(evidence_pool)}건  (axis별: {by_axis})")
    for method_name, payload in legacy_facts.items():
        text = (payload["text"] or "")[:110]
        print(f"  · [{payload['axis']}] {method_name}")
        print(f"      {text}")

    # 2c. 직전 라운드 prior signals (Phase D 자동조회 예정)
    print("\n2c. 직전 라운드 prior signals (Phase D: storage 자동 조회 예정)")
    prior_comments: list[dict] = []
    prior_qa: list[dict] = []
    print(f"  human_comments: {len(prior_comments)}건  "
          f"qa_gaps: {len(prior_qa)}건  (현재는 항상 빈 리스트)")

    # ───────── Stage 3. 플래너 동작 ─────────
    _header("[3] 플래너 동작 (planner_v3, initial 모드)")

    node = make_planner_v3_node(str(DB_PATH))
    initial_state = {
        "asset_id": "try_planner_v3",
        "asset_summary": asset_summary,
        "asset_identifiers": asset_identifiers,
        "evidence_pool": evidence_pool,
        "prefetched_facts": legacy_facts,
        "prior_human_comments": prior_comments,
        "prior_qa_gaps": prior_qa,
    }
    out = node(initial_state)

    # ───────── Stage 4. 결과 ─────────
    _header("[4] 결과")

    mind_map_dict = out.get("mind_map", {})
    out_evidence = out.get("evidence_pool", {})
    outline = out.get("section_outline", [])
    log = out.get("discourse_log", [])

    print(f"\nMind Map ({len(mind_map_dict)} concepts):")
    print(_pretty_mind_map(mind_map_dict, out_evidence))

    print(f"\nLegacy Section Outline ({len(outline)}개, graph_v2 호환):")
    for sec in outline:
        print(f"\n  ## [{sec['axis']}] {sec['title']}")
        print(f"      Q: {sec['question']}")

    print(f"\nDiscourse Log:")
    print(json.dumps(log, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
