"""음악 도메인 외부 데이터 소스 클라이언트 (자산 고유).

real_estate/external/와 평행한 구조 — 음악저작권에서만 쓰는 외부 API 클라이언트를
소스별 서브패키지로 둔다. 공통(shared) 클라이언트는 infra/external/에 있다.

서브패키지:
    musicbrainz/ — 작품·녹음·창작자 (ISRC↔ISWC, User-Agent 필수, 1 req/s)
    deezer/      — ISRC 트랙 조회 (rank, 키 불필요)
    lastfm/      — track.getInfo (listeners/playcount)
"""
