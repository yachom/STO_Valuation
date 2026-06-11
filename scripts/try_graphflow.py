"""graphflow 시험 진입점 (PR-A: Seeder).

주소 1줄 → LocationResolver → 자산 스코프 그래프 적재.

사용 예:
    # Neo4j 없이 그래프 모양/카드 눈검사 (SQLite만, 카카오/Juso 키 필요)
    python -m scripts.try_graphflow --seed-only --dry-run "서울특별시 강남구 강남대로 396"

    # Neo4j에 적재 (docker-compose up -d neo4j 후)
    python -m scripts.try_graphflow --seed-only "서울특별시 강남구 강남대로 396"

    # 적재 + 카드 임베딩/벡터 인덱스까지 (모델 ~1.5GB 로드)
    python -m scripts.try_graphflow --seed-only --embed "서울특별시 강남구 강남대로 396"

필요 env (.env): KAKAO_API_KEY, JUSO_API_KEY (+ Neo4j 적재 시 NEO4J_*)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from infra.env import load_env
from real_estate.graphflow.seeder import collect_scope
from real_estate.location import LocationResolver

DB_PATH = str(Path(__file__).resolve().parent.parent
              / "real_estate" / "database" / "DB" / "real_estate.db")

_DEFAULT_ADDRESS = "서울특별시 강남구 강남대로 396"


def _print_scope(scope) -> None:
    s = scope.summary()
    print("\n" + "=" * 70)
    print(f"ScopeGraph — 노드 {s['nodes']}개, 엣지 {s['edges']}개")
    print(f"  라벨별: {s['by_label']}")
    print(f"  관계별: {s['by_rel']}")
    print("=" * 70)

    print("\n── 노드 (카드 텍스트 눈검사) ──")
    by_label: dict[str, list] = {}
    for n in scope.nodes:
        by_label.setdefault(n.label, []).append(n)
    for label in by_label:
        print(f"\n[{label}]")
        for n in by_label[label]:
            print(f"  · {n.key}")
            print(f"      {n.card_text}")

    print("\n── 엣지 ──")
    for e in scope.edges:
        extra = f"  {e.props}" if e.props else ""
        print(f"  ({e.src_label} {e.src_key}) -[{e.rel}]-> ({e.dst_label} {e.dst_key}){extra}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="graphflow Seeder 시험")
    ap.add_argument("address", nargs="?", default=_DEFAULT_ADDRESS, help="평가 대상 주소")
    ap.add_argument("--seed-only", action="store_true", help="(PR-A 단계) 적재만 수행")
    ap.add_argument("--dry-run", action="store_true",
                    help="Neo4j 없이 ScopeGraph만 출력 (SQLite만 사용)")
    ap.add_argument("--embed", action="store_true",
                    help="적재 후 카드 임베딩 + 벡터 인덱스 (모델 로드)")
    ap.add_argument("--radius", type=int, default=800, help="배후수요 반경(m)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    load_env()

    kakao = os.environ.get("KAKAO_API_KEY")
    juso = os.environ.get("JUSO_API_KEY")
    if not kakao or not juso:
        print("ERROR: .env에 KAKAO_API_KEY, JUSO_API_KEY가 필요합니다.", file=sys.stderr)
        return 2

    print(f"주소 정규화 중: {args.address}")
    resolver = LocationResolver(kakao_api_key=kakao, juso_api_key=juso)
    location = resolver.resolve(args.address)
    print(f"  → {location.road_address or location.jibun_address} "
          f"| bd_mgt_sn={location.bd_mgt_sn} | {location.sigungu} bcode={location.bcode}")

    scope = collect_scope(location, DB_PATH, radius_m=args.radius)
    _print_scope(scope)

    if args.dry_run:
        print("(dry-run: Neo4j 적재 생략)")
        return 0

    from real_estate.graphflow.graph.driver import close_neo4j_driver, get_neo4j_driver
    from real_estate.graphflow.seeder import seed

    print("Neo4j 적재 중...")
    driver = get_neo4j_driver()
    try:
        result = seed(scope, driver, embed=args.embed)
        print(f"  적재 완료: {result}")
    finally:
        close_neo4j_driver()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
