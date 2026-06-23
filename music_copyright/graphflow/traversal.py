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
    """Work에서 hops 이내 노드 eid 집합 (QA 스코프 한정, 설계 §D11)."""
    with driver.session() as s:
        r = s.run(
            f"""
            MATCH (w:Work {{work_id: $wid}})
            MATCH (w)-[*0..{int(hops)}]-(m)
            RETURN collect(DISTINCT elementId(m)) AS eids
            """,
            wid=work_id,
        ).single()
        return set(r["eids"]) if r else set()
