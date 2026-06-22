# 변경 이력 (Changelog)

이 프로젝트의 주요 변경을 버전별로 기록한다. 버전은
[유의적 버전(SemVer)](https://semver.org/lang/ko/)을 따르고, 각 버전은
**annotated git 태그**(`vX.Y.Z`)로 마킹된다.

## 버전화 방식 — 되돌아갈 수 있는 체크포인트

- **태그 = 복원 가능한 스냅샷.** `git checkout vX.Y.Z` 로 그 버전의 코드 상태로 복귀한다.
- **MAJOR.MINOR.PATCH**: 하위호환을 깨면 MAJOR, 하위호환 새 기능은 MINOR, 버그 수정은 PATCH.
- 버전 비교 `git diff v0.2.1 v0.3.0` · 목록 `git tag -l` · GitHub의 Tags/Releases에도 표시.
- 새 버전 내는 절차: 변경을 논리 단위로 커밋 → 이 파일(CHANGELOG) 갱신 →
  `git tag -a vX.Y.Z -m "..."` → `git push origin vX.Y.Z`.
- 태그는 **불변**으로 둔다(푸시된 태그는 옮기지 않음). 새 내용은 다음 버전으로.

---

## [v0.3.0] — 2026-06-22 · STORM/Co-STORM 담론 큐레이션

보고서의 데이터 확장(EXPAND)을 STORM/Co-STORM 담론 구조로 고도화. **기본 동작
(`curation.mode=static`)은 불변·하위호환** — 신규 담론 경로는 `config.json`의
`curation.mode`(`static` | `moderated` | `discourse`) 플래그 뒤로 격리된다.
논문 충실도 약 90%, 상도동 505-1 end-to-end 실측검증 완료.

> 이 버전은 v0.2.1(pre-graphflow) 이후의 **graphflow 그래프 네이티브 보고서 시스템
> 재구축**을 포함한다 — 상세 설계·결정 로그는 `docs/design/graph_native_flow.md`
> (D1~D22)와 `docs/design/agentic_curation.md` 참고.

### Added
- **담론 큐레이션** (`real_estate/graphflow/discourse.py`): 관점 발견(Perspectivist)
  → 페르소나 라운드테이블 Q&A(Questioner↔Researcher — 공유 다이제스트로 전문가들이
  서로의 발견 위에 질문) → 아웃라인(Outliner).
- **Moderator** (`moderator.py`): 커버리지 + 수집 사실 **스니펫** 기반 unknown-unknowns
  질문 + GAP→미니EXPAND 재진입 (Co-STORM 모더레이터).
- **커버리지 측정** (`coverage.py`): 자산 척추 커버리지 + 사실 표본 — 순수 Cypher,
  명시 앵커 스코프(이웃 자산 누출 방지), Langfuse score 적재.
- **STORM-native LangGraph 토폴로지** (`workflow.py`): `tenant` 뒤 조건부 라우팅 +
  담론 사이클(`perspectives → discourse_round ⟲ moderator → outline`)을 노드로 —
  워크플로우 토폴로지에서 STORM 구조가 가시화(체크포인트·스트리밍·interrupt 토대).
- **Streamlit 큐레이션 모드 + 🧠 담론 탭** (`scripts/app.py`): UI에서 discourse 모드
  구동, 관점·페르소나·라운드테이블 Q&A·아웃라인 표시.
- 시험 스크립트: `try_moderator` · `try_discourse` · `try_full_report`.

### Changed
- **검색어 생성** (`navigator.py`): 짧은 검색어 규칙 + 길이 가드(`_MAX_QUERY_WORDS`).
  긴 문장형 쿼리가 네이버에서 0건 나던 문제 교정.
- **집필** (`writer.py`, `report.py`): `write_axis`가 담론 아웃라인을 입력으로 수용해
  그 논점 순서로 집필(grounding 불변 — 증거 없는 논점은 '근거 부족' 명시).

### Fixed
- **검색 모달리티** (`harvester.py`): 0건 쿼리 축약 재시도(`_broaden_query`) +
  Tavily `topic`을 `news`→`general`로(한국 로컬 웹·블로그·실거래 — 뉴스가 못 보던
  소형 상가 정보). 실측에서 신규 facts 26 → 263으로.

---

## [v0.2.1] — 2026-06-05
- Anthropic prompt caching 헬퍼(`cache_block`/`cached_system`) + 6개 에이전트 system
  캐싱 적용 (PR-23).

## [v0.2.0] — 2026-06-05
- BASE_LLM 매니저(provider 분기) + Langfuse + 로컬 사이드카·SQL 트레이싱 자급화.
  주소 입력 → 상가 평가, 보고서 trace/report_id, retriever+scout, 도메인 위계·critic
  gate (PR-17~21).

> v0.2.x는 graphflow 재구축 이전의 에이전트 스택 기준이다.
