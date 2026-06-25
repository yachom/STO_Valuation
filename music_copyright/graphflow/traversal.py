"""GraphWalk 래퍼 (음악저작권) — infra walk 엔진에 도메인 KEY_PROP 주입.

엔진(가중 BFS 증거 수집)은 infra.graph.walk에 있다. real_estate/graphflow/
traversal.py와 동일하게, 여기엔 음악 KEY_PROP를 주입하는 얇은 래퍼 + 앵커 해소
헬퍼만 둔다(infra 코드 복제 없음). 확장·집필·QA가 모두 같은 walk를 쓴다(설계 §5).

NodeRef/EdgeStep/EvidencePath/WalkPolicy는 재노출 — 호출부 호환.

⚠️ TODO(domain): 앵커 라벨은 placeholder Work, 키는 work_id다. QA scope/벡터
앵커는 도메인 확장 시 real_estate처럼 정밀화한다.
"""

from __future__ import annotations

import logging

from neo4j import Driver

from infra.graph.walk import EdgeStep, EvidencePath, NodeRef  # noqa: F401  (호환 재노출)
from infra.graph.walk import _expand as _infra_expand
from infra.graph.walk import load_nodes as _infra_load_nodes
from infra.graph.walk import walk as _infra_walk
from music_copyright.graphflow.graph.schema import KEY_PROP
from music_copyright.graphflow.policies import WalkPolicy  # noqa: F401  (호환 재노출)

logger = logging.getLogger("music_copyright.walk")


# ───────────────────────────────────────────────────────────
# 엔진 래퍼 — infra.graph.walk 엔진에 도메인 KEY_PROP를 주입한다.
# ───────────────────────────────────────────────────────────
def load_nodes(driver: Driver, eids: list[str]) -> dict[str, NodeRef]:
    return _infra_load_nodes(driver, eids, KEY_PROP)


def _expand(driver: Driver, eids: list[str]) -> dict:
    """frontier eid들의 1홉 이웃(양방향). KEY_PROP 주입 후 infra 엔진에 위임."""
    return _infra_expand(driver, eids, KEY_PROP)


def walk(
    driver: Driver,
    anchor_eids: list[str],
    policy: WalkPolicy,
    *,
    max_hops: int = 3,
    k: int = 12,
) -> list[EvidencePath]:
    """앵커에서 max_hops 이내 가중 BFS로 증거 경로 top-k 반환 (KEY_PROP 주입)."""
    return _infra_walk(driver, anchor_eids, policy, key_props=KEY_PROP,
                       max_hops=max_hops, k=k)


# ───────────────────────────────────────────────────────────
# 앵커 해소 (설계 §5.6) — placeholder Work 앵커
# ───────────────────────────────────────────────────────────
def work_anchor_eid(driver: Driver, work_id: str) -> str | None:
    """보고서 집필용 고정 앵커 — Work 노드 elementId."""
    with driver.session() as s:
        r = s.run(
            "MATCH (w:Work {work_id: $wid}) RETURN elementId(w) AS eid",
            wid=work_id,
        ).single()
        return r["eid"] if r else None


def scope_eids(driver: Driver, work_id: str, hops: int) -> set[str]:
    """Work에서 hops 이내 노드 eid 집합 (QA 스코프 한정, 설계 §D11).

    허브-인지(hub-aware): 경로 *중간*에 공유 허브(Artist·레이블·플랫폼·수상·다른 Work)가
    있으면 그 경로는 버린다 — 그래야 'Dynamite→공유아티스트→다른 곡→그 곡의 Fact' 같은
    교차오염이 스코프(특히 Fact 벡터검색)에 안 샌다. 이 곡 고유 수집물(Query/WebDoc/Fact/
    Entity)은 허브를 거치지 않아 그대로 들어온다. walk의 expandable 게이트와 같은 원리.
    """
    with driver.session() as s:
        r = s.run(
            f"""
            MATCH path=(w:Work {{work_id: $wid}})-[*0..{int(hops)}]-(m)
            WHERE all(x IN nodes(path)[1..-1] WHERE
                NOT (x:Work OR x:Artist OR x:Rightsholder OR x:Platform
                     OR x:Award OR x:Chart OR x:Certification OR x:SocialMetric))
            RETURN collect(DISTINCT elementId(m)) AS eids
            """,
            wid=work_id,
        ).single()
        return set(r["eids"]) if r else set()


# ───────────────────────────────────────────────────────────
# Semantic 검색 입력 — 축 의도 코사인 / 출처 보강 (real_estate 미러)
# ───────────────────────────────────────────────────────────
def semantic_sims(
    driver: Driver,
    work_id: str,
    purpose_text: str,
    *,
    scope_hops: int = 4,
) -> dict[str, float]:
    """Work 스코프 노드 card_embedding vs 축 의도 임베딩 코사인 → eid별 [0,1] 정규화.

    SemanticPolicy 입력 — walk 노드배수에 곱해 '엣지 종류 상수'가 못 보는 '이 노드가
    축 관심사와 의미적으로 가까운가'를 데이터로 반영(가중치 표 의존 제거). e5 코사인은
    스프레드가 좁아 스코프 내 min-max 정규화로 변별력을 준다. 결정적(임베딩·스코프 고정).
    """
    from infra.graph.embedder import embed_one

    pvec = embed_one(purpose_text)
    pnorm = sum(x * x for x in pvec) ** 0.5 or 1.0
    with driver.session() as s:
        rows = s.run(
            f"""
            MATCH (w:Work {{work_id: $wid}})-[*0..{int(scope_hops)}]-(m)
            WHERE m.card_embedding IS NOT NULL
              AND (NOT m:Work OR m.work_id = $wid)   // 타 작품 노드 누출 차단(공유 허브)
            RETURN DISTINCT elementId(m) AS eid, m.card_embedding AS emb
            """,
            wid=work_id,
        ).data()
    sims: dict[str, float] = {}
    for r in rows:
        emb = r["emb"]
        if not emb:
            continue
        dot = sum(a * b for a, b in zip(pvec, emb))
        enorm = sum(x * x for x in emb) ** 0.5 or 1.0
        sims[r["eid"]] = dot / (pnorm * enorm)
    if sims:                                   # 스코프 내 min-max 정규화 (변별력)
        lo, hi = min(sims.values()), max(sims.values())
        rng = (hi - lo) or 1.0
        sims = {k: (v - lo) / rng for k, v in sims.items()}
    logger.info("semantic_sims(%s): %d nodes scored", purpose_text[:24], len(sims))
    return sims


def corroboration_scores(
    driver: Driver,
    work_id: str,
    *,
    scope_hops: int = 4,
) -> dict[str, float]:
    """스코프 노드별 '독립 출처 수' — 여러 출처가 보강할수록 강한 노드(SemanticPolicy 입력).

    Fact 총량(에코·재탕 포함)이 아니라 그 Fact들이 추출된 WebDoc의 distinct source 수를
    센다(같은 출처 재탕은 1). Work 스코프 내 Fact만 — 타 작품 보강이 새지 않게.
    """
    with driver.session() as s:
        rows = s.run(
            f"""
            MATCH (w:Work {{work_id: $wid}})-[*0..{int(scope_hops)}]-(f:Fact)
                  -[:MENTIONS]->(n)
            MATCH (f)-[:EXTRACTED_FROM]->(d:WebDoc)
            WITH n, count(DISTINCT coalesce(d.source, d.url)) AS srcs
            WHERE srcs > 0
            RETURN elementId(n) AS eid, srcs
            """,
            wid=work_id,
        ).data()
    out = {r["eid"]: float(r["srcs"]) for r in rows}
    logger.info("corroboration_scores(%s): %d nodes with sources", work_id, len(out))
    return out
