# STO Valuation

STO(토큰증권) 기초자산의 **감정평가(Sentiment Valuation)** 를 LLM 온톨로지로
정형화하는 파이프라인. 사양: `doc/product.md`.

본 저장소는 현재 **뼈대(skeleton)** 단계입니다. 모든 함수/클래스는 시그니처만
정의되어 있고 본문은 `NotImplementedError` 입니다.

## 폴더 구조

```
common/    # 공통 인프라 + 자산-불문 최소 추상
projects/  # 자산별 구체화 (commercial = MVP, music = placeholder)
airflow/   # 배치 DAG + 진입 스크립트
```

자세한 구조는 `doc/product.md` 와 `.claude/plans/5-lazy-turing.md` 참고.

## 의존성 그래프 (불변)

```
Layer 8   airflow/dags
Layer 7   airflow/scripts, composition.py, cli.py
Layer 6   projects/<x>/{flows, agents, tools, services, clients, mcp}
Layer 5   projects/<x>/domain
Layer 4   common/{flows, agents}
Layer 3   common/{services, tools}
Layer 2   common/{clients, llm, mcp, repositories, graph, embedding}
Layer 1   common/ports
Layer 0   common/{domain, errors, utils}
Cross-cutting: common/{observability, config}
```

위 레이어는 아래만 import. 역방향 0. `common` 은 `projects` 를 모름.
자산 간 cross import 금지.

## 작성 규칙

**Pure function first** — 의존성이 없으면 모듈 함수, 있으면 클래스로 승격.
클래스 메서드는 pure 함수 합성의 얇은 오케스트레이터.

- 상속: `BaseClient`, `BaseTool`, `BaseAgent`, `BaseLLM`, `BaseMCPServer`
- 합성: services, flows (clients/llm/repos 주입)

## 4계층 외부 연결

| 계층 | 책임 |
|---|---|
| clients | 외부 1회 호출 (인증·재시도·rate-limit) |
| services | clients 합성 + domain 변환 |
| tools | services 의 LLM 래퍼 (선별적) |
| mcp | tools 의 외부 노출 |

## 실행 (예정)

```bash
# 온디맨드 — 주소 분석
python -m cli analyze-address --asset commercial --address "서울 강남구 ..."

# 배치 — 뉴스 수집
python -m cli collect-news --asset commercial --date 2026-05-13
```

## 새 자산 추가

1. `projects/<자산>/` 디렉토리 생성 (commercial 구조 미러)
2. `profile.py`, `domain/`, `clients/`, `services/`, `tools/`, `agents/`, `flows/` 채움
3. `composition.py` 의 `PROFILE_REGISTRY` 에 빌더 등록

`common/` 무수정.
