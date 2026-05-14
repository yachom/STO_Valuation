"""부동산 STO 사회적 가치 평가용 온톨로지 정의."""

from __future__ import annotations

ENTITY_TYPES = [
    "Asset",          # 자산 (건물·단지)
    "Organization",   # 시행/시공/운영/임차 법인
    "Person",         # 개인 (대표, 관계자)
    "Location",       # 지명, 지하철역, 행정구역
    "Event",          # 매각, 증축, 인증, 소송, 임대차, 화재
    "Issue",          # ESG/안전/소음/민원 등 부정적 이슈
    "Metric",         # 연면적, 임대료, 공실률 등 수치 신호
]

RELATION_TYPES = [
    "OWNED_BY",       # Asset → Organization
    "OPERATED_BY",    # Asset → Organization
    "TENANT_OF",      # Organization → Asset
    "LOCATED_IN",     # Asset → Location
    "MENTIONED_IN",   # Entity → NewsArticle
    "HAS_EVENT",      # Asset → Event
    "HAS_ISSUE",      # Asset → Issue
    "HAS_METRIC",     # Asset → Metric
    "AFFECTS_AXIS",   # Event/Issue → axis(ESG|AWARENESS|VITALITY)
]

AXES = ["ESG", "AWARENESS", "VITALITY"]
SENTIMENTS = ["positive", "neutral", "negative"]


SYSTEM_PROMPT = f"""당신은 부동산 뉴스에서 엔티티와 관계를 추출하는 정보 추출기다.
대상 자산에 관련된 정보만 추출하라. 자산과 무관한 일반 시장 기사는 무시.

## 엔티티 타입
{', '.join(ENTITY_TYPES)}

## 관계 타입
{', '.join(RELATION_TYPES)}

## 축
{', '.join(AXES)} (이벤트/이슈는 어떤 축에 영향을 주는지 표기)

## 감정
{', '.join(SENTIMENTS)}

## 출력 스키마 (반드시 JSON 객체만)
{{
  "asset_relevance": "high|medium|low|none",
  "summary_ko": "1~2문장 요약",
  "sentiment": "positive|neutral|negative",
  "entities": [
    {{"id": "e1", "type": "Asset|...", "name": "이름", "attrs": {{...}}}}
  ],
  "relations": [
    {{"src": "e1", "rel": "OWNED_BY|...", "dst": "e2", "evidence": "원문 짧은 인용"}}
  ],
  "axis_signals": [
    {{"axis": "ESG|AWARENESS|VITALITY", "polarity": "positive|negative|neutral",
      "strength": 1-5 정수, "note": "왜 이 축에 영향"}}
  ]
}}

asset_relevance가 'none'이면 entities/relations/axis_signals는 빈 배열로.
"""
