"""infra/graph — 자산 무관 그래프 메커니즘.

Neo4j driver, 로컬 임베더, 그래프 스키마 메커니즘(GraphSchemaSpec), 벡터 인덱스
연산, GraphWalk 엔진. 자산 패키지(<asset>/graphflow)는 도메인 데이터(라벨/키/
프로파일)를 주입하는 얇은 래퍼만 둔다 — "infra = 파라미터화된 메커니즘,
자산 = 도메인 주입 래퍼".
"""
