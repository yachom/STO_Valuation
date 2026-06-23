"""음악 Seeder 검증 — resolve → collect_scope (→ seed).

real_estate의 try_graphflow --seed-only에 대응.
    --scope-only : Neo4j 없이 collect_scope 결과만 출력 (오프라인 검증)
    (기본)       : collect_scope + Neo4j 적재 (NEO4J_* 필요, docker-compose 가동)
    --embed      : 적재 시 카드 임베딩까지 (모델 로드 ~1.5GB)

실행:
    $ python -m scripts.try_music_seed --title "Dynamite" --artist "BTS" --scope-only
    $ python -m scripts.try_music_seed --title "Dynamite" --artist "BTS"
"""

from __future__ import annotations

import argparse
import os

from infra.env import load_env
from music_copyright.graphflow.seeder import collect_scope, seed
from music_copyright.resolver import WorkResolver


def main():
    p = argparse.ArgumentParser(description="음악 Seeder 검증")
    p.add_argument("--title", default="Dynamite")
    p.add_argument("--artist", default="BTS")
    p.add_argument("--scope-only", action="store_true", help="Neo4j 없이 스코프만 출력")
    p.add_argument("--embed", action="store_true", help="적재 시 카드 임베딩까지")
    args = p.parse_args()

    load_env()
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    ua = os.environ.get("MUSICBRAINZ_USER_AGENT")
    lastfm = os.environ.get("LASTFM_API_KEY")
    if not (cid and secret and ua):
        print("에러: SPOTIFY_CLIENT_ID/SECRET, MUSICBRAINZ_USER_AGENT 필요")
        return

    # 1. 식별
    resolver = WorkResolver(spotify_client_id=cid, spotify_client_secret=secret,
                            musicbrainz_user_agent=ua)
    print(f"질의: {args.artist} - {args.title}\n해소 중...")
    ident = resolver.resolve(title=args.title, artist=args.artist)
    print(f"  → work_id={ident.work_id}  ISWC={ident.iswc}  ISRC={ident.isrc}\n")

    # 2. 스코프 수집 (구조화 API)
    print("스코프 수집 중 (MusicBrainz/Deezer/Last.fm)...")
    scope = collect_scope(ident, lastfm_api_key=lastfm, mb_user_agent=ua)

    print("\n=== ScopeGraph 요약 ===")
    print(" ", scope.summary())
    print("\n--- 노드 ---")
    for n in scope.nodes:
        print(f"  [{n.label}] {n.key}")
        print(f"      card: {n.card_text}")
    print("\n--- 엣지 ---")
    for e in scope.edges:
        print(f"  ({e.src_label}:{e.src_key}) -[{e.rel} {e.props}]-> ({e.dst_label}:{e.dst_key})")

    if args.scope_only:
        print("\n(--scope-only: Neo4j 적재 생략)")
        return

    # 3. Neo4j 적재
    from infra.graph.driver import get_neo4j_driver
    print("\nNeo4j 적재 중...")
    driver = get_neo4j_driver()
    try:
        result = seed(scope, driver, embed=args.embed)
        print("적재 완료:", result)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
