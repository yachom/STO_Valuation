# Agentic Graph Autonomy Decision

> 작성일: 2026-06-26  
> 대상: `music_copyright` graphflow  
> 구현 상태: 1차 수직 슬라이스 반영 (`agentic_graph` stage)

## 1. 프로젝트 목적

음악 저작권 평가 그래프의 목적은 단순 RAG가 아니라, 수집된 웹/정형 근거를
`Work` 중심 지식그래프로 적재하고, 평가 목적에 맞는 멀티홉 탐색을 통해 보고서 근거를
구성하는 것이다.

현재 평가 축은 다음 두 트랙으로 나뉜다.

- 자산가치: `influence`, `popularity`, `sustainability`
- 사회적 가치: `social_value`

사회적 가치 축은 곡 자체의 메시지·공적 활용뿐 아니라, 아티스트/가수의 공적 행위,
팬덤의 집단 행동, 문화외교·공동체 파급까지 포함한다. 다만 아티스트/팬덤 근거를 곡의
내재 메시지로 과대 귀속하면 안 된다.

## 2. 발견한 핵심 문제

문제는 데이터 수집 자체보다, 수집 이후 그래프를 구축하고 탐색하는 단계에 있었다.

1. 웹 수집은 `Fact`, `WebDoc`, `Entity`를 충분히 만든다.
2. 하지만 미해소 `Entity`가 `Fandom`, `Award`, `Chart`, `Agency`, `Cause`,
   `PublicUse` 같은 타입 노드로 승격되지 않으면 walk가 의미 있는 경로로 쓰지 못한다.
3. 보고서 작성 단계는 그래프에 이미 있는 근거도 축 목적과 맞게 가져오지 못하는 경우가
   있었다.
4. 실패 사례를 고치면서 검색 템플릿, 토큰 리스트, 특정 사회 신호 키워드가 코드에
   늘어났다.

이 상태를 계속 규칙으로 보정하면, 시스템은 일반 음악 저작권 평가기가 아니라 특정
검증 케이스(Dynamite/BTS)에 맞춘 규칙 엔진으로 굳어질 위험이 있다.

## 3. 하드코딩 흔적

대표적인 흔적은 다음과 같다.

- `navigator.py`: 사회적 가치 목적일 때 `사회공헌`, `문화외교`, `팬덤 기부` 같은
  템플릿을 강제로 보강한다.
- `consolidate.py`: 팬덤/기획사/수상/차트/장르/미디어 이벤트 등을 토큰 리스트로 판정한다.
- `composer.py`: 축별 evidence 회수에서 키워드 그룹, 출처 감점, 맥락 감점 규칙이 늘었다.
- `prompts.py`: writer/synthesizer가 사회적 가치 과대 귀속을 막도록 강하게 지시한다.

이 규칙들은 필요한 안전장치지만, 새 작품·새 장르·새 사회적 맥락을 만날수록 계속
수동 추가가 필요해진다.

## 4. 검토한 대안

### A. 규칙 계속 확장

- 장점: 재현성이 높고 디버깅이 쉽다.
- 단점: 케이스별 토큰·템플릿이 누적되어 과적합한다.
- 판단: 단기 버그 픽스에는 유효하지만, 그래프 DB를 쓰는 핵심 이유와 맞지 않는다.

### B. LLM이 그래프를 완전 자율 수정

- 장점: 새 엔티티와 관계를 유연하게 발견한다.
- 단점: schema drift, hallucinated edge, 잘못된 고가중 경로, 교차오염 위험이 크다.
- 판단: 현재 단계에서는 과하다. 특히 `WON`, `CHARTED_ON`, `MANAGED_BY` 같은 엣지는
  보고서 점수에 직접 영향을 주므로 임의 커밋하면 안 된다.

### C. Agent proposal + deterministic validator

- 장점: LLM은 그래프 상태를 보고 후보를 제안하고, 코드는 schema/provenance/confidence를
  검증한다.
- 단점: validator 설계가 필요하고, 완전 자율보다 느리게 일반화된다.
- 판단: 현재 프로젝트에 가장 적합하다.

## 5. 결정

LLM/agent의 역할을 "판단자"가 아니라 "제안자"로 둔다.

규칙의 역할은 "최종 판단"에서 "검증자/가드레일"로 낮춘다.

| 영역 | 자율성 | 결정 |
|---|---:|---|
| 검색 질문 생성 | 높음 | 목적·커버리지·그래프 공백 기반으로 agent가 제안 |
| 관점 발견 | 높음 | STORM/Co-STORM 담론 경로에서 agent가 발견 |
| 그래프 read 탐색 | 높음 | read-only tool/walk 기반이면 안전 |
| Fact/WebDoc 적재 | 중간-높음 | provenance가 있으므로 자동 적재 가능 |
| Entity 승격 | 중간 | agent proposal 후 validator 통과 시 일부만 자동 |
| 고가중 구조 엣지 | 낮음-중간 | 강한 근거 없으면 review 후보 |
| 스키마 변경 | 낮음 | agent는 제안만, 사람/설정 승인 필요 |
| 최종 등급 | 낮음 | cited claim + rubric 기반 제한 |

## 6. 이번 구현 범위

신규 stage: `agentic_graph`

위치:

```text
static:    seed -> expand -> consolidate -> agentic_graph -> write_* -> synthesize -> finalize
discourse: seed -> perspectives -> discourse_round/moderator -> outline
           -> consolidate -> agentic_graph -> write_* -> synthesize -> finalize
```

구현 파일:

- `music_copyright/graphflow/agentic_graph.py`
- `music_copyright/graphflow/workflow.py`
- `music_copyright/graphflow/report.py`
- `music_copyright/graphflow/prompts.py`
- `config/music_graphflow.json`
- `config/music_prompts.json`
- `config/models.json`

## 7. Agent가 받는 그래프 상태

`agentic_graph` stage는 다음을 요약해 agent에게 준다.

- 평가 대상 `Work`와 performer `Artist`
- 축별 커버리지 요약
- 스코프 내 기존 typed node refs
- unresolved `Entity` 목록
- 각 Entity를 언급한 `Fact`와 source tier/url/title

Agent는 이 상태만 보고 `GraphProposal` JSON을 낸다. 직접 Cypher는 금지된다.

## 8. 제안 action

허용 action은 네 가지다.

- `promote_entity`: 강한 근거가 있는 Entity를 typed node로 승격 제안
- `same_as_candidate`: 기존 스코프 노드와 같은 대상으로 보이면 후보 연결
- `mark_review`: 유용하지만 자동 승격이 위험한 대상
- `reject`: 연도·순위·일반명사·노이즈

자동 커밋 대상은 1차로 다음 라벨로 제한했다.

- `Fandom`
- `SocialTheme`
- `Cause`
- `PublicUse`

이 라벨들은 사회적 가치 평가에 직접 필요하고, Work/Artist 앵커가 명확하며,
근거 Fact를 같이 연결하면 감사 가능성이 유지된다.

## 9. Validator 규칙

자동 승격은 다음 조건을 통과해야 한다.

- proposal confidence가 `agentic_graph.min_conf` 이상
- 입력에 존재하는 `entity_id`와 `fact_id`만 사용
- target label이 허용 목록에 있음
- 근거 Fact가 최소 1개 이상 있음
- 근거 텍스트에 평가 대상 Work 또는 performer Artist 맥락이 있음
- target label별 최소 의미 신호가 있음
  - `Fandom`: 팬덤/팬클럽/fan/fandom 등
  - `SocialTheme`: 메시지/가사/다양성/정체성/위로/공동체 등
  - `Cause`: 기부/자선/공익/캠페인/사회운동/시위 등
  - `PublicUse`: United Nations/유엔/문화외교/공공/교육/기념 등

주의: 과거 오탐을 피하기 위해 `un` 같은 짧은 약어 단독 매칭은 쓰지 않는다.

## 10. Commit 방식

자동 승격 시:

1. typed node를 deterministic key로 `MERGE`
2. Work 또는 Artist에 schema edge 연결
3. supporting Fact를 새 typed node에 `MENTIONS`로 연결
4. 원 Entity에는 `agent_promoted`, `merged_into_label`, `merged_into_key`,
   `agent_rationale`, `agent_confidence`를 남김
5. 새 label embedding 갱신 시도

`same_as_candidate`는 기존 노드에 `SAME_AS_CANDIDATE`만 만든다.

자동 승격이 위험하거나 validator를 통과하지 못한 proposal은 `review_candidate`로 남긴다.

## 11. 운영 플래그

`config/music_graphflow.json`:

```json
"agentic_graph": {
  "enabled": true,
  "mode": "proposal_first",
  "min_conf": 0.72,
  "scope_hops": 4,
  "max_entities": 24,
  "facts_per_entity": 5,
  "max_proposals": 12,
  "auto_commit_labels": ["Fandom", "SocialTheme", "Cause", "PublicUse"]
}
```

코드 기본값은 `enabled=false`다. 프로젝트 설정 파일에서만 켰다.

## 12. 기대 효과

- 특정 작품/아티스트명에 맞춘 검색 템플릿 추가를 줄인다.
- `Entity` 무덤에 남던 사회적 가치 신호를 typed node로 일부 승격한다.
- writer가 `SocialTheme`, `Cause`, `PublicUse`, `Fandom` 경로를 evidence로 회수할 수 있다.
- 실패해도 review metadata가 남아 다음 설계 개선 입력이 된다.

## 13. 남은 과제

- `Award`, `Chart`, `Agency`, `MediaEvent`, `ViralTrend` 자동 승격은 아직 보류.
- proposal 품질을 trace에서 집계해 false positive/false negative를 봐야 한다.
- `agentic_graph` 결과를 보고서 JSON에 별도 diagnostic으로 넣을지 결정해야 한다.
- 사람 검토 UI 또는 Cypher review script가 필요하다.
- 충분한 표본에서 static vs proposal_first vs discourse 경로를 A/B 비교해야 한다.
