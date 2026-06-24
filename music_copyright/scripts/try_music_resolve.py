"""음악 인식자(WorkResolver) 라이브 검증 스크립트.

제목+아티스트 → Spotify 검색 → ISRC → MusicBrainz → ISWC 체인이 실데이터로
도는지 확인한다 (Neo4j 불필요 — 식별만).

실행:
    python -m music_copyright.scripts.try_music_resolve --title "Dynamite" --artist "BTS"
    python -m music_copyright.scripts.try_music_resolve --title "좋니" --artist "윤종신"
"""

from __future__ import annotations

import argparse
import os

from infra.env import load_env
from music_copyright.resolver import WorkResolver


def main():
    parser = argparse.ArgumentParser(description="음악 인식자(WorkResolver) 검증")
    parser.add_argument("--title", default="Dynamite", help="곡 제목")
    parser.add_argument("--artist", default="BTS", help="아티스트명")
    args = parser.parse_args()

    load_env()
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    ua = os.environ.get("MUSICBRAINZ_USER_AGENT")
    missing = [k for k, v in {"SPOTIFY_CLIENT_ID": cid, "SPOTIFY_CLIENT_SECRET": secret,
                              "MUSICBRAINZ_USER_AGENT": ua}.items() if not v]
    if missing:
        print("에러: .env에 다음 키가 없습니다 →", ", ".join(missing))
        return

    resolver = WorkResolver(spotify_client_id=cid, spotify_client_secret=secret,
                            musicbrainz_user_agent=ua)
    print(f"질의: {args.artist} - {args.title}\n해소 중...\n")
    ident = resolver.resolve(title=args.title, artist=args.artist)

    print("=== 해소 결과 ===")
    print(f"  resolution     : {ident.resolution}")
    print(f"  title / artist : {ident.title} / {ident.artist}")
    print(f"  ISRC (녹음)    : {ident.isrc}")
    print(f"  ISWC (작품)    : {ident.iswc}   ← 자산 앵커")
    print(f"  release        : {ident.release_date} (year={ident.release_year})")
    print(f"  work_id / asset_id : {ident.work_id} / {ident.asset_id}")
    print(f"  spotify_url    : {ident.spotify_url}")


if __name__ == "__main__":
    main()
