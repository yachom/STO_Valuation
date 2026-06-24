"""음악 Seeder 검증 — resolve → collect_scope (→ seed).

    --scope-only : 음악 Neo4j 없이 collect_scope 결과만 출력 (오프라인 검증)
    (기본)       : collect_scope + 음악 Neo4j 적재 (MUSIC_NEO4J_*, docker neo4j-music)
    --embed      : 적재 시 카드 임베딩까지

실행:
    python -m music_copyright.scripts.try_music_seed --title "Dynamite" --artist "BTS" --scope-only
    python -m music_copyright.scripts.try_music_seed --title "Dynamite" --artist "BTS"
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

    resolver = WorkResolver(spotify_client_id=cid, spotify_client_secret=secret,
                            musicbrainz_user_agent=ua)
    print(f"질의: {args.artist} - {args.title}\n해소 중...")
    ident = resolver.resolve(title=args.title, artist=args.artist)
    print(f"  → work_id={ident.work_id}  ISWC={ident.iswc}  ISRC={ident.isrc}\n")

    print("스코프 수집 중 (MusicBrainz/Deezer/Last.fm)...")
    scope = collect_scope(ident, lastfm_api_key=lastfm, mb_user_agent=ua)

    print("\n=== ScopeGraph 요약 ===")
    print(" ", scope.summary())
    for n in scope.nodes:
        print(f"  [{n.label}] {n.key}\n      card: {n.card_text}")
    for e in scope.edges:
        print(f"  ({e.src_label}:{e.src_key}) -[{e.rel} {e.props}]-> ({e.dst_label}:{e.dst_key})")

    if args.scope_only:
        print("\n(--scope-only: Neo4j 적재 생략)")
        return

    from music_copyright.graphflow.graph.driver import close_music_driver, get_music_driver
    print("\n음악 Neo4j 적재 중...")
    driver = get_music_driver()
    try:
        result = seed(scope, driver, embed=args.embed)
        print("적재 완료:", result)
    finally:
        close_music_driver()


if __name__ == "__main__":
    main()
