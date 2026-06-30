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
    # ── 영향력 (비평·문화적 영향, 수상·미디어 언급, 샘플·커버) ──
    "influence": {
        E.WON: 0.95,            # 수상이 영향력의 핵심 신호
        E.FEATURED_IN: 0.6,     # 방송·공연·페스티벌 노출
        E.SPREAD_AS: 0.55,      # 챌린지·밈으로 재호명
        E.SAMPLED_BY: 0.6,      # 이 곡을 샘플링한 작품 = 상호텍스트적 영향
        E.COVERED_BY: 0.55,     # 이 곡을 커버한 아티스트 = 문화적 파급
        E.CHARTED_ON: 0.6,
        E.BY_ARTIST: 0.7,       # 아티스트 위상이 비평적 영향에 기여
        E.HAS_GENRE: 0.35,
        E.PART_OF_RELEASE: 0.3,
        E.MANAGED_BY: 0.35,
        E.HAS_FANDOM: 0.45,
        E.MENTIONS: 0.8,        # 비평/미디어 언급
        E.ABOUT: 0.6,
        E.EXTRACTED_FROM: 0.5,
        E.EXTRACTED_BY: 0.55,
        E.HAS_METRIC: 0.4,      # 관심도(pageviews 등)
        E.CERTIFIED: 0.5,
        E.HELD_BY: 0.3,
        E.DISTRIBUTED_ON: 0.2,
        E.SAME_AS_CANDIDATE: 0.1,
    },
    # ── 대중성·팬덤 (대중 도달·팬덤 활동) ──
    "popularity": {
        E.CHARTED_ON: 0.95,     # 차트 성적이 대중 도달의 핵심
        E.HAS_METRIC: 0.9,      # 소셜/스트리밍 지표(팔로워·조회·pageviews)
        E.DISTRIBUTED_ON: 0.85,  # 스트리밍 플랫폼 도달
        E.CERTIFIED: 0.8,       # 인증(판매·스트리밍 누적) = 대중 도달
        E.SPREAD_AS: 0.85,      # 숏폼·챌린지 확산
        E.BY_ARTIST: 0.7,       # 아티스트 팬덤
        E.HAS_FANDOM: 0.9,      # 팬덤 공동체가 대중 도달·참여의 핵심 신호
        E.FEATURED_IN: 0.55,
        E.PART_OF_RELEASE: 0.4,
        E.HAS_GENRE: 0.25,
        E.MANAGED_BY: 0.3,
        E.MENTIONS: 0.7,
        E.COVERED_BY: 0.5,      # 커버 = 바이럴/대중 침투
        E.SAMPLED_BY: 0.45,
        E.ABOUT: 0.5,
        E.EXTRACTED_FROM: 0.5,
        E.EXTRACTED_BY: 0.55,
        E.WON: 0.4,
        E.HELD_BY: 0.2,
        E.SAME_AS_CANDIDATE: 0.1,
    },
    # ── 지속성 (롱테일 스트리밍·재생 지속·세대 전이) ──
    "sustainability": {
        E.DISTRIBUTED_ON: 0.9,   # 롱테일 스트리밍 지속
        E.CERTIFIED: 0.85,       # 인증 = 장기 누적 성과
        E.HELD_BY: 0.7,          # 권리 안정성이 지속 수익에 직결
        E.MANAGED_BY: 0.6,       # 매니지먼트/레이블 운영 역량
        E.HAS_FANDOM: 0.45,      # 팬덤 유지력
        E.PART_OF_RELEASE: 0.45, # 앨범/싱글 맥락
        E.SPREAD_AS: 0.4,
        E.HAS_GENRE: 0.35,
        E.FEATURED_IN: 0.3,
        E.COVERED_BY: 0.6,       # 세대를 넘는 커버 = 세대 전이 신호
        E.BY_ARTIST: 0.6,        # 카탈로그 세대 전이
        E.CHARTED_ON: 0.5,
        E.HAS_METRIC: 0.5,
        E.MENTIONS: 0.7,
        E.ABOUT: 0.5,
        E.EXTRACTED_FROM: 0.5,
        E.EXTRACTED_BY: 0.55,
        E.SAMPLED_BY: 0.4,
        E.WON: 0.4,
        E.SAME_AS_CANDIDATE: 0.1,
    },
    # ── 사회적 가치 (사회적 의제·메시지, 사회운동·공익 연계, 대표성·공동체 파급) ──
    # 상업 신호(차트·스트리밍·인증)는 의도적으로 낮춘다 — 이 축의 변별은 사회 의제·
    # 운동·공익 활용·미디어 담론이다. SemanticPolicy가 축 의도로 관련 노드를 부상시킨다.
    "social_value": {
        E.ADDRESSES: 0.95,      # 작품이 다루는 사회적 의제 = 핵심 신호
        E.SUPPORTED: 0.9,       # 사회운동·시위·공익/자선 캠페인 연계
        E.USED_IN: 0.85,        # 공익·교육·기념·문화외교 활용
        E.MENTIONS: 0.8,        # 사회적 메시지에 대한 미디어·담론 언급
        E.HAS_FANDOM: 0.65,     # 팬덤의 공익·캠페인·공동체 파급
        E.SPREAD_AS: 0.5,       # 해시태그·챌린지·밈의 사회적 확산
        E.FEATURED_IN: 0.45,
        E.HAS_GENRE: 0.25,
        E.PART_OF_RELEASE: 0.2,
        E.MANAGED_BY: 0.25,
        E.ABOUT: 0.6,
        E.EXTRACTED_FROM: 0.55,
        E.EXTRACTED_BY: 0.6,
        E.BY_ARTIST: 0.65,      # 가수/아티스트의 사회적 위상·공적 행위
        E.WON: 0.4,             # 평화·사회공헌상 등 일부 수상은 사회적
        E.COVERED_BY: 0.4,      # 세대·집단을 잇는 합창·커버(공동체 결속)
        E.HAS_METRIC: 0.25,
        E.CHARTED_ON: 0.2,      # 상업 신호는 의도적으로 낮춤
        E.DISTRIBUTED_ON: 0.2,
        E.CERTIFIED: 0.2,
        E.HELD_BY: 0.2,
        E.SAMPLED_BY: 0.3,
        E.SAME_AS_CANDIDATE: 0.1,
    },
    # ── QA 베이스라인 (LLMPolicy boost의 출발점 + 미등록 동적 축 폴백) ──
    "qa_base": {
        E.WON: 0.7,
        E.CHARTED_ON: 0.7,
        E.DISTRIBUTED_ON: 0.7,
        E.BY_ARTIST: 0.7,
        E.PART_OF_RELEASE: 0.5,
        E.HELD_BY: 0.6,
        E.MANAGED_BY: 0.5,
        E.HAS_FANDOM: 0.5,
        E.HAS_GENRE: 0.5,
        E.FEATURED_IN: 0.5,
        E.SPREAD_AS: 0.5,
        E.MENTIONS: 0.7,
        E.COVERED_BY: 0.6,
        E.SAMPLED_BY: 0.6,
        E.CERTIFIED: 0.7,
        E.HAS_METRIC: 0.7,
        E.ADDRESSES: 0.6,
        E.SUPPORTED: 0.6,
        E.USED_IN: 0.6,
        E.ABOUT: 0.6,
        E.EXTRACTED_FROM: 0.5,
        E.EXTRACTED_BY: 0.55,
        E.SAME_AS_CANDIDATE: 0.1,
    },
}

# 보고서 집필이 도는 기본 축 (Composer가 순회) — axes.BASE_AXES와 일치
REPORT_AXES = ("influence", "popularity", "sustainability", "social_value")
