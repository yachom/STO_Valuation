"""주소 1개 → planner 노드 단독 실행.

목적: planner가 prefetch facts 위에서 어떤 outline을 짜는지 확인.
다른 에이전트(retriever, analyst, synthesizer)는 호출하지 않는다.

전제:
    - real_estate.db 적재 완료 (commercial_sale, REB, BEEC, G-SEED, transit, apt)
    - .env: ANTHROPIC_API_KEY, KAKAO_API_KEY, JUSO_API_KEY

사용:
    python -m scripts.try_planner "서울특별시 강남구 테헤란로 152"
"""

import json
import logging
import os
import sys
from pathlib import Path

from infra.env import load_env
from real_estate.location import LocationResolver
from real_estate.report.agents.planner import make_planner_node

DB_PATH = Path("real_estate/database/DB/real_estate.db")


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

    address = " ".join(sys.argv[1:]).strip() or "서울특별시 강남구 테헤란로 152"

    # 1) 주소 정규화 → 식별자/요약 구성
    resolver = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"],
    )
    location = resolver.resolve(address)
    asset_summary = (
        f"주소: {address}\n"
        f"도로명: {getattr(location, 'road_address', '')}\n"
        f"지번: {getattr(location, 'jibun_address', '')}\n"
        f"건물관리번호: {getattr(location, 'bd_mgt_sn', '')}\n"
        f"좌표: ({getattr(location, 'latitude', '')}, "
        f"{getattr(location, 'longitude', '')})\n"
        f"시군구: {getattr(location, 'sigungu', '')}"
    )
    bcode = getattr(location, "bcode", None) or ""
    hcode = getattr(location, "hcode", None) or ""
    asset_identifiers = {
        "bd_mgt_sn": getattr(location, "bd_mgt_sn", None),
        "latitude": getattr(location, "latitude", None),
        "longitude": getattr(location, "longitude", None),
        "sigungu": getattr(location, "sigungu", None),
        "sigungu_cd": bcode[:5] if bcode else None,
        # 카카오 hcode는 10자리, 서울 OD admin_dong은 8자리 — 앞 8자리 매칭.
        "adstrd_cd": hcode[:8] if hcode else None,
    }

    print("\n=== Asset Identifiers ===")
    print(json.dumps(asset_identifiers, ensure_ascii=False, indent=2))

    # 2) planner 노드 단독 호출
    node = make_planner_node(str(DB_PATH))
    initial = {
        "asset_id": "try_planner",
        "asset_summary": asset_summary,
        "asset_identifiers": asset_identifiers,
    }
    out = node(initial)

    # 3) 결과 출력
    facts = out.get("prefetched_facts", {})
    print(f"\n=== Prefetched Facts ({len(facts)}건) ===")
    for name, payload in facts.items():
        print(f"\n[{payload.get('axis')}] {name}")
        print(f"  ref:  {payload.get('ref')}")
        print(f"  text: {payload.get('text')}")

    outline = out.get("section_outline", [])
    print(f"\n=== Section Outline ({len(outline)}개) ===")
    for sec in outline:
        print(f"\n## [{sec['axis']}] {sec['title']}  (id={sec['section_id']})")
        print(f"  Q: {sec['question']}")


if __name__ == "__main__":
    main()
