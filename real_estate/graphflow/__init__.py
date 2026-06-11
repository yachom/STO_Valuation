"""graphflow — 그래프 네이티브 보고서/QA 에이전트 흐름.

주소 1줄 입력 → 자산 스코프 그래프 적재(Seeder) → 멀티 홉 확장(EXPAND) →
3축 정성 분석 보고서 집필 → 발행자 상호작용 수정 + 동일 그래프 기반 QA.

설계 문서: docs/design/graph_native_flow.md
기존 real_estate/report/ 스택과 무관하게 데이터 축(location/database/external/
repository + infra)만 공유한다.
"""
