"""부동산 전용 정형 데이터 커넥터 (인터넷 검색으로 안 나오는 자산별 소스).

설계 원칙: infra/external에는 인터넷 검색(ddgs/tavily/naver_search/youtube)만 둔다.
자산 특수 커넥터는 각 자산 하위(여기)에 둔다 — 공통 수집 단계에 plug-in.
"""
