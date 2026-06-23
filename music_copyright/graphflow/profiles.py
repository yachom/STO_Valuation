"""축별 엣지 가중치 프로파일 (음악저작권) — StaticPolicy의 통과 우선순위 표.

real_estate/graphflow/profiles.py를 미러링한다. 가중치는 엣지 **종류**에 붙는
통과 우선순위다(개별 노드 아님). walk가 같은 1홉 이웃이라도 어떤 종류의 관계로
닿았느냐에 따라 줍는 순서를 정한다. 곱셈 구조라 깊이 페널티가 내장된다.

절대값보다 순서(tier)가 중요: 상(0.8~1.0)/중(0.4~0.7)/하(0.1~0.3).

⚠️ TODO(domain): tier 배치는 placeholder 가설이다. 골든 작품으로 walk 결과를
눈검사하며 정련한다. 3축 = influence(영향력)·popularity(대중성)·sustainability(지속성).
"""

from __future__ import annotations

from music_copyright.graphflow.graph.schema import EDGE_TYPES as E

# 프로파일에 없는 엣지 종류의 기본 가중치 — 하드 프루닝 대신 강한 페널티.
DEFAULT_WEIGHT = 0.05


PROFILES: dict[str, dict[str, float]] = {
    # ── 영향력 (비평·문화적 영향, 수상·미디어 언급) ──
    "influence": {
        E.WON: 0.95,            # 수상이 영향력의 핵심 신호
        E.CHARTED_ON: 0.6,
        E.BY_ARTIST: 0.7,       # 아티스트 위상이 비평적 영향에 기여
        E.MENTIONS: 0.8,        # 비평/미디어 언급
        E.ABOUT: 0.6,
        E.EXTRACTED_FROM: 0.5,
        E.HELD_BY: 0.3,
        E.DISTRIBUTED_ON: 0.2,
        E.SAME_AS_CANDIDATE: 0.1,
    },
    # ── 대중성·팬덤 (대중 도달·팬덤 활동) ──
    "popularity": {
        E.CHARTED_ON: 0.95,     # 차트 성적이 대중 도달의 핵심
        E.DISTRIBUTED_ON: 0.85,  # 스트리밍 플랫폼 도달
        E.BY_ARTIST: 0.7,       # 아티스트 팬덤
        E.MENTIONS: 0.7,
        E.ABOUT: 0.5,
        E.EXTRACTED_FROM: 0.5,
        E.WON: 0.4,
        E.HELD_BY: 0.2,
        E.SAME_AS_CANDIDATE: 0.1,
    },
    # ── 지속성 (롱테일 스트리밍·재생 지속·세대 전이) ──
    "sustainability": {
        E.DISTRIBUTED_ON: 0.9,   # 롱테일 스트리밍 지속
        E.HELD_BY: 0.7,          # 권리 안정성이 지속 수익에 직결
        E.BY_ARTIST: 0.6,        # 카탈로그 세대 전이
        E.CHARTED_ON: 0.5,
        E.MENTIONS: 0.7,
        E.ABOUT: 0.5,
        E.EXTRACTED_FROM: 0.5,
        E.WON: 0.4,
        E.SAME_AS_CANDIDATE: 0.1,
    },
    # ── QA 베이스라인 (LLMPolicy boost의 출발점) ──
    "qa_base": {
        E.WON: 0.7,
        E.CHARTED_ON: 0.7,
        E.DISTRIBUTED_ON: 0.7,
        E.BY_ARTIST: 0.7,
        E.HELD_BY: 0.6,
        E.MENTIONS: 0.7,
        E.ABOUT: 0.6,
        E.EXTRACTED_FROM: 0.5,
        E.SAME_AS_CANDIDATE: 0.1,
    },
}

# 보고서 집필이 도는 3축 (Composer가 순회)
REPORT_AXES = ("influence", "popularity", "sustainability")
