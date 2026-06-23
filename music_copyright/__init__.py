"""music_copyright — 음악저작권 자산 패키지 (두 번째 자산, infra 합성 검증용).

real_estate와 동급 top-level 자산. infra/ 메커니즘(그래프 엔진·walk·BaseAgent·
설정 로더)을 **합성**하고, 음악저작권 도메인(노드/엣지/카드/프로파일/프롬프트)만
이 패키지가 소유한다. infra 코드는 복제하지 않는다.

⚠️ 도메인 미확정(스캐폴드): 자산 단위는 placeholder `work_id`(앵커 라벨 Work),
가치 3축은 사회·문화가치형 — influence(영향력)·popularity(대중성·팬덤)·
sustainability(지속성). 데이터 수집은 웹검색 우선(ddgs/naver/tavily). 도메인이
확정되면 # TODO(domain) 표시 지점을 채운다.
"""
