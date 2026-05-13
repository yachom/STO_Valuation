# 06. Extension Guide — 새 자산 추가

## 약속

새 자산 추가는 **`common/` 무수정**으로 끝나야 한다. `projects/<자산>/`
한 디렉토리를 만들고 `composition.py` 에 한 줄 등록한다.

## 단계별

자산 이름 예: `art` (미술품).

### 1. 폴더 골조

`projects/commercial/` 를 그대로 미러:

```
projects/art/
├── __init__.py
├── profile.py
├── domain/
│   ├── __init__.py
│   ├── targets.py
│   ├── factors.py
│   ├── ontology.py
│   ├── signals.py
│   ├── claims.py
│   └── relations.py
├── clients/
├── services/
├── tools/
├── agents/
├── flows/
├── mcp/
├── config/
│   ├── search_groups.yaml
│   └── llm.yaml
└── utils/
```

### 2. `AssetClass` 추가

`common/domain/asset.py`:

```python
class AssetClass(str, Enum):
    COMMERCIAL = "commercial"
    MUSIC = "music"
    ART = "art"             # ← 추가
```

이건 사실상 *유일한 common 변경*이고, enum 확장은 인터페이스 변경이
아니므로 영향 0.

### 3. `profile.py`

```python
from common.domain.asset import AssetClass, AssetProfile

ART_PROFILE = AssetProfile(
    asset_class=AssetClass.ART,
    display_name="Art (paintings/sculptures)",
    description="...",
    config_dir="projects/art/config",
)
```

### 4. `domain/`

- `targets.py`: `class PaintingTarget(Target)` — `.identifier` 가 어떤 키인가?
  (작품 아카이브 번호? authenticator hash? 결정 후 명명)
- `factors.py`: 미술품 평가 요인 사전. 예: `auction_recency`, `artist_market_trend`,
  `provenance_strength`. `FactorAxis` 도 자산별로 정의.
- `ontology.py`: 미술품 신호의 축. 부동산의 6축과 다를 수 있음.
- `signals.py`, `claims.py`, `relations.py`: 부동산 모델 참고해 자산화.

### 5. `clients/`

각 외부 API 마다 `class XClient(BaseClient)`. 한 endpoint = 한 method.
응답은 dict 또는 client-local pydantic.
**domain 을 import 하지 않는다.**

### 6. `services/`

- `initial_collector.py`: 자산의 초기 메타데이터 수집 시퀀스 (있다면).
- `ontology_tagging.py`: `class ArtOntologyTagger(OntologyTagger)` —
  `common.ports.news.OntologyTagger` 구현. LLM 호출 + 자산 축으로 매핑.
- `graph_signal.py`: `class ArtGraphSignalWriter(GraphSignalWriter)` —
  tag→GraphNode/Edge.
- `report_builder.py`: 그래프 → 리포트 텍스트.

### 7. `tools/`

agent 가 쓸 만한 service 만 `BaseTool` 래핑. 1:1 강제 X.

```python
class CollectArtMetadataTool(BaseTool):
    name = "collect_art_metadata"
    description = "..."
    input_schema = {...}
    def __init__(self, service: ArtInitialCollectorService): ...
    def run(self, **kwargs): ...
```

### 8. `agents/`

`PlannerBase`/`RetrieverBase`/`CriticBase` 를 상속. 자산 특화 시스템 프롬프트.

### 9. `flows/analyze_<asset>.py`

`common.flows.exploration.build_exploration_graph(...)` 위에서 자산 에이전트로
조립.

### 10. `mcp/server.py` (선택)

이 자산의 tool 들을 MCP 로 노출하고 싶으면 `BaseMCPServer` 상속.
필요 없으면 빈 채로 둠 (`pass`).

### 11. `config/`

- `search_groups.yaml`: 자산용 뉴스 쿼리 그룹.
- `llm.yaml`: 역할별 모델 매핑.

### 12. `composition.py` 등록

```python
def build_art(settings: Any) -> ArtContext:
    juso = ...          # 자산 client 들 인스턴스화
    registry = load_registry(settings, "projects/art/config/llm.yaml")
    planner = ArtPlanner(llm=registry["planner"], tools=[...])
    ...
    return ArtContext(profile=ART_PROFILE, flows=AnalyzeArtFlow(...), ...)


PROFILE_REGISTRY: dict[str, Callable[[Any], object]] = {
    "commercial": build_commercial,
    "music":      build_music,
    "art":        build_art,   # ← 추가
}
```

### 13. (선택) `airflow/dags/<asset>_news_daily.py`

배치가 필요하면 DAG 추가. 실 로직은 `airflow/scripts/run_news_pipeline.py`
가 이미 자산 dispatch 를 처리.

### 14. tests

`tests/projects/art/` 미러. 최소한 smoke import 추가.

## 검증

```bash
python -c "import projects.art"                 # import 성공
python -c "from composition import PROFILE_REGISTRY; print(PROFILE_REGISTRY['art'])"
pytest tests/smoke                              # 모든 모듈 import + composition wire
```

## 잘못 가는 경우 (안티패턴)

- ❌ `common/domain` 에 `PaintingTarget` 추가 — 자산 domain 이지 common 아님.
- ❌ `projects/commercial` 에서 `projects/art` 의 무엇인가 import — 자산 격리 위반.
- ❌ `common/services/news/pipeline.py` 에 자산-종속 분기 추가 — 자산은 ports 로 주입.
- ❌ 자산 client 안에서 자산 domain import — clients 는 domain 무지.
- ❌ 자산 tool 안에 비즈니스 로직 — service 로 옮기고 tool 은 래핑만.
