"""GraphWalk 시험 진입점 (PR-B).

주소 → (자동 시드) → walk 엔진으로 증거 경로 수집. 두 가지 모드:

    # 보고서 집필 경로: 3축 StaticPolicy로 자산 앵커에서 walk (결정적)
    python -m scripts.try_walk "서울특별시 강남구 강남대로 396"

    # QA 경로: 질문 벡터 앵커 + qa_base walk (임베딩 필요 → 자동 --embed 시드)
    python -m scripts.try_walk "서울특별시 강남구 강남대로 396" --question "1층에 카페 입점 가능성 있어?"

    # 재현성 게이트: 같은 walk 2회 → 동일 출력 확인
    python -m scripts.try_walk "..." --repro

필요 env (.env): KAKAO_API_KEY, JUSO_API_KEY, NEO4J_*
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from infra.env import load_env
from real_estate.graphflow.policies import StaticPolicy
from real_estate.graphflow.profiles import REPORT_AXES
from real_estate.graphflow.seeder import asset_id_for, collect_scope, seed
from real_estate.graphflow.traversal import (
    asset_anchor_eid,
    vector_anchors,
    walk,
)
from real_estate.location import LocationResolver

DB_PATH = str(Path(__file__).resolve().parent.parent
              / "real_estate" / "database" / "DB" / "real_estate.db")

_DEFAULT_ADDRESS = "서울특별시 강남구 강남대로 396"


def _print_paths(title: str, paths) -> None:
    print(f"\n{'='*70}\n{title} — {len(paths)} paths\n{'='*70}")
    for i, p in enumerate(paths, 1):
        print(f"\n[E{i}] score={p.score:.4f}  hops={p.hops}  → {p.tail.label}:{p.tail.key}")
        for line in p.text().splitlines():
            print(f"     {line}")


def _serialize(paths) -> str:
    """재현성 비교용 — 점수+경로 직렬화."""
    return "\n".join(f"{p.score:.6f}|{'>'.join(n.key for n in p.nodes)}" for p in paths)


def main() -> int:
    ap = argparse.ArgumentParser(description="GraphWalk 시험 (PR-B)")
    ap.add_argument("address", nargs="?", default=_DEFAULT_ADDRESS)
    ap.add_argument("--question", help="QA 모드: 질문 벡터 앵커 walk")
    ap.add_argument("--max-hops", type=int, default=3)
    ap.add_argument("-k", type=int, default=8, help="경로 top-k")
    ap.add_argument("--repro", action="store_true", help="walk 2회 동일성 확인")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    load_env()
    kakao, juso = os.environ.get("KAKAO_API_KEY"), os.environ.get("JUSO_API_KEY")
    if not (kakao and juso):
        print("ERROR: .env에 KAKAO_API_KEY, JUSO_API_KEY 필요", file=sys.stderr)
        return 2

    print(f"주소 정규화: {args.address}")
    location = LocationResolver(kakao_api_key=kakao, juso_api_key=juso).resolve(args.address)
    asset_id = asset_id_for(location)
    print(f"  asset_id={asset_id}")

    from infra.graph.driver import close_neo4j_driver, get_neo4j_driver
    driver = get_neo4j_driver()
    try:
        need_embed = bool(args.question)   # 벡터 앵커는 임베딩 필요
        print(f"시드 적재 중 (embed={need_embed})...")
        seed(collect_scope(location, DB_PATH), driver, embed=need_embed)

        if args.question:
            anchor_eids = vector_anchors(
                driver, args.question, k=3, scope_asset_id=asset_id, scope_hops=args.max_hops,
            )
            print(f"\n질문: {args.question}\n벡터 앵커 {len(anchor_eids)}개")
            policy = StaticPolicy("qa_base")
            paths = walk(driver, anchor_eids, policy, max_hops=args.max_hops, k=args.k)
            _print_paths(f"QA walk (qa_base)", paths)
            if args.repro:
                again = walk(driver, anchor_eids, policy, max_hops=args.max_hops, k=args.k)
                ok = _serialize(paths) == _serialize(again)
                print(f"\n재현성: {'동일 ✓' if ok else '불일치 ✗'}")
            return 0

        # 보고서 집필 경로: 3축 StaticPolicy
        anchor = asset_anchor_eid(driver, asset_id)
        if anchor is None:
            print("ERROR: Asset 노드를 찾을 수 없음", file=sys.stderr)
            return 1
        repro_ok = True
        for axis in REPORT_AXES:
            policy = StaticPolicy(axis)
            paths = walk(driver, [anchor], policy, max_hops=args.max_hops, k=args.k)
            _print_paths(f"[{axis}] StaticPolicy walk", paths)
            if args.repro:
                again = walk(driver, [anchor], policy, max_hops=args.max_hops, k=args.k)
                same = _serialize(paths) == _serialize(again)
                repro_ok = repro_ok and same
                print(f"  재현성({axis}): {'동일 ✓' if same else '불일치 ✗'}")
        if args.repro:
            print(f"\n전체 재현성: {'통과 ✓' if repro_ok else '실패 ✗'}")
        return 0
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    raise SystemExit(main())
