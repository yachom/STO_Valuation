"""발행자 피드백 합의 루프 — 이벤트 진입점 + 그래프 영속화 (설계 §10.4/§10.5, D13/D14).

핵심 불변식: **합의(agreed) 전에는 보고서가 절대 변하지 않는다.** 피드백이 오면
먼저 그래프 증거와 대조한 Assessment(인용 의무)를 회신하고, 발행자가 수용해야
비로소 외과적 수정 → 새 버전. 증거 대조·수정 자체는 Reviser 에이전트(reviser.py)가
하고, 이 모듈은 그 위의 상태머신·그래프 적재·라이프사이클만 담당한다.

상태머신 (Feedback.status — 그래프가 체크포인트):
    open → assessed → ┬ agreed → applied   (수용 → 수정 → 새 버전)
                      ├ disputed            (재반박 누적, 미합의 보류 = 보고서 불변)
                      └ withdrawn           (철회 = 보고서 불변)

이벤트 구동 진입점 (보고서 생성 워크플로우와 별개 라이프사이클):
    submit_feedback(...)  → Feedback+Assessment 적재, 평가 회신
    resolve_feedback(...) → accept/rebut/withdraw/dispute 처리

그래프: (:Feedback)-[:TARGETS]->(Claim|Section), (:Assessment)-[:REPLIES_TO]->
(:Feedback), (:Assessment)-[:CITES]->(증거). 스레드로 영구 보존.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

from neo4j import Driver

from infra.agent.tracing import emit, node_scope
from real_estate.graphflow.composer import paths_to_evidence
from real_estate.graphflow.graph.schema import EDGE_TYPES as E
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.report import (
    asset_card_text,
    bump_version,
    rewrite_report_graph,
    save_report_files,
)
from real_estate.graphflow.reviser import Reviser
from real_estate.graphflow.seeder import ScopeGraph, SeedEdge, seed, write_edges
from real_estate.graphflow.traversal import EvidencePath, load_nodes

logger = logging.getLogger("graphflow.feedback")

# 상태
OPEN, ASSESSED, AGREED, APPLIED, DISPUTED, WITHDRAWN = (
    "open", "assessed", "agreed", "applied", "disputed", "withdrawn")

_VALID_KINDS = {"edit", "challenge", "enrich"}


# ───────────────────────────────────────────────────────────
# 타깃 해소 헬퍼
# ───────────────────────────────────────────────────────────
def _target_label(target_id: str) -> str:
    if target_id.startswith("clm_"):
        return L.CLAIM
    if target_id.startswith("sec_"):
        return L.REPORT_SECTION
    return L.CLAIM       # block_id는 claim을 통해 간접 — TARGETS는 claim/section만


def _resolve_target_claim(report: dict, target_id: str) -> tuple[str, dict] | None:
    """target_id(claim/block/section) → 대표 claim_id + claim dict."""
    claims = report["claims"]
    if target_id in claims:
        return target_id, claims[target_id]
    # block_id → 그 블록의 첫 claim
    for cid, cl in claims.items():
        if cl.get("block_id") == target_id:
            return cid, cl
    # section_id → 그 섹션의 첫 claim
    if target_id.startswith("sec_"):
        axis = target_id[4:]
        for cid, cl in claims.items():
            if cl.get("axis") == axis:
                return cid, cl
    return None


def _collected_evidence(driver, collected_by) -> list:
    """이번 피드백에서 수집된(collected_by) Fact → Evidence (첨부 자료 대조용)."""
    with driver.session() as s:
        eids = [r["eid"] for r in s.run(
            f"MATCH (f:{L.FACT} {{collected_by: $cb}}) RETURN elementId(f) AS eid",
            cb=collected_by)]
    if not eids:
        return []
    nodes = load_nodes(driver, eids)
    return paths_to_evidence(driver, [EvidencePath([n], [], 1.0) for n in nodes.values()])


# ───────────────────────────────────────────────────────────
# 그래프 적재
# ───────────────────────────────────────────────────────────
def _write_feedback(driver, report_id, feedback_id, target_id, kind, comment):
    sg = ScopeGraph()
    sg.add_node(L.FEEDBACK, feedback_id, {
        "report_id": report_id, "target_id": target_id, "kind": kind,
        "comment": comment, "status": OPEN,
        "created_at": datetime.now().isoformat(timespec="seconds")})
    seed(sg, driver)
    write_edges(driver, [
        SeedEdge(L.FEEDBACK, feedback_id, E.TARGETS, _target_label(target_id), target_id)])


def _write_assessment(driver, feedback_id, assessment, evidence, round_no):
    aid = "asm_" + uuid4().hex[:10]
    sg = ScopeGraph()
    sg.add_node(L.ASSESSMENT, aid, {
        "stance": assessment.stance, "rationale": assessment.rationale,
        "proposal": assessment.proposal, "round": round_no,
        "created_at": datetime.now().isoformat(timespec="seconds")})
    seed(sg, driver)
    edges = [SeedEdge(L.ASSESSMENT, aid, E.REPLIES_TO, L.FEEDBACK, feedback_id)]
    ev_by_index = {ev.index: ev for ev in evidence}
    for i in assessment.cited_evidence_indexes:
        ev = ev_by_index.get(i)
        if ev is not None:
            label, key = ev.ref.split(":", 1)
            edges.append(SeedEdge(L.ASSESSMENT, aid, E.CITES, label, key))
    write_edges(driver, edges)
    return aid


def _set_status(driver, feedback_id, status, **extra):
    sets = "".join(f", f.{k} = ${k}" for k in extra)
    with driver.session() as s:
        s.run(f"MATCH (f:{L.FEEDBACK} {{feedback_id: $fid}}) SET f.status = $st{sets}",
              fid=feedback_id, st=status, **extra)


def _reconnect_target(driver, feedback_id, new_claim_id):
    """수정으로 claim_id가 바뀌면 끊긴 TARGETS를 새 대표 claim으로 재연결."""
    if not new_claim_id:
        return
    with driver.session() as s:
        s.run(
            f"""
            MATCH (f:{L.FEEDBACK} {{feedback_id: $fid}})
            MERGE (c:{L.CLAIM} {{claim_id: $cid}})
            MERGE (f)-[:{E.TARGETS}]->(c)
            """,
            fid=feedback_id, cid=new_claim_id)


def _next_round(driver, feedback_id) -> int:
    with driver.session() as s:
        r = s.run(
            f"MATCH (a:{L.ASSESSMENT})-[:{E.REPLIES_TO}]->"
            f"(:{L.FEEDBACK} {{feedback_id: $fid}}) RETURN count(a) AS n",
            fid=feedback_id).single()
        return (r["n"] if r else 0) + 1


# ───────────────────────────────────────────────────────────
# 이벤트 진입점
# ───────────────────────────────────────────────────────────
def submit_feedback(driver: Driver, report: dict, asset_id: str, *,
                    target_id: str, kind: str, comment: str,
                    attachments: list[dict] | None = None) -> dict:
    """발행자 피드백 제출 → Assessment 회신 (보고서는 아직 불변).

    attachments: [{"text": ..., "provider": ...}] — 발행자 제공 자료. UserDoc으로
    인입돼(provisional tier) 일반 증거와 동일 파이프라인 통과 후 Assessment의
    증거 대조에 포함된다 (맹신도 배척도 아님, §10.5).

    반환: {feedback_id, stance, rationale, proposal, status, _target_claim_id}.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"kind는 {_VALID_KINDS} 중 하나 — {kind}")
    feedback_id = "fb_" + uuid4().hex[:10]
    with node_scope("reviser_assess", kind=kind):
        resolved = _resolve_target_claim(report, target_id)
        if resolved is None:
            raise ValueError(f"target_id 미발견: {target_id}")
        _, target_claim = resolved
        _write_feedback(driver, report["report_id"], feedback_id,
                        target_id, kind, comment)
        # 첨부 자료 인입 (UserDoc, provisional) — 평가 전에 그래프에 반영
        extra_evidence = []
        if attachments:
            from real_estate.graphflow.harvester import Harvester
            h = Harvester()
            for att in attachments:
                h.ingest_user_doc(driver, asset_id=asset_id, text=att["text"],
                                  provider=att.get("provider", "발행자 제공"),
                                  collected_by=feedback_id,
                                  context=f"피드백 맥락: {comment[:100]}")
            extra_evidence = _collected_evidence(driver, feedback_id)
        reviser = Reviser()
        assessment, evidence = reviser.assess(
            driver, report, asset_id, target_claim, kind, comment,
            extra_evidence=extra_evidence)
        _write_assessment(driver, feedback_id, assessment, evidence, round_no=1)
        _set_status(driver, feedback_id, ASSESSED, stance=assessment.stance)
        emit("feedback_assessed", feedback_id=feedback_id, kind=kind,
             stance=assessment.stance, attachments=len(attachments or []))
    return {"feedback_id": feedback_id, "stance": assessment.stance,
            "rationale": assessment.rationale, "proposal": assessment.proposal,
            "status": ASSESSED, "_target_claim_id": resolved[0]}


def _apply_accept(driver: Driver, report: dict, asset_id: str, *,
                  feedback_id: str, target_claim_id: str, kind: str, comment: str,
                  out_dir: str, asset_card: str | None) -> dict:
    """accept(합의) → 외과적 수정(edit/challenge/enrich) → 새 버전 발행.

    resolve_feedback의 accept 분기를 분리 — resolve_feedback은 결정 디스패치만 담당.
    """
    _set_status(driver, feedback_id, AGREED)
    with node_scope("reviser_apply", kind=kind):
        if asset_card is None:
            asset_card = asset_card_text(driver, asset_id)
        reviser = Reviser()
        _, target_claim = _resolve_target_claim(report, target_claim_id) or (None, None)
        if kind == "edit":
            evidence = reviser._gather_evidence(driver, asset_id, target_claim,
                                                comment, max_hops=3)
            report = reviser.apply_edit(report, asset_card, target_claim_id,
                                        comment, evidence)
        elif kind == "challenge":
            report = reviser.apply_challenge(driver, report, asset_id, target_claim_id,
                                             comment, asset_card, collected_by=feedback_id)
        elif kind == "enrich":
            report = reviser.apply_enrich(driver, report, asset_id, target_claim_id,
                                          comment, asset_card, collected_by=feedback_id)

        new_target = report.pop("_last_target_claim_id", None)
        bump_version(report, notes=f"[{kind}] {comment[:60]}")
        save_report_files(report, out_dir)
        rewrite_report_graph(driver, report)
        _reconnect_target(driver, feedback_id, new_target)   # TARGETS 보존
        _set_status(driver, feedback_id, APPLIED,
                    resulted_in_version=report["version_id"])
        emit("feedback_applied", feedback_id=feedback_id, kind=kind,
             version=report["version_id"])
    return {"status": APPLIED, "report_changed": True,
            "version": report["version_id"], "report": report}


def resolve_feedback(driver: Driver, report: dict, asset_id: str, *,
                     feedback_id: str, target_claim_id: str, kind: str,
                     comment: str, decision: str, out_dir: str,
                     asset_card: str | None = None,
                     rebut_comment: str | None = None) -> dict:
    """발행자 결정 처리.

    decision: accept(수용→수정→새 버전) | withdraw(철회) | dispute(미합의 보류)
              | rebut(재반박 → 새 라운드 Assessment, 보고서 불변).
    """
    if decision == "withdraw":
        _set_status(driver, feedback_id, WITHDRAWN)
        emit("feedback_withdrawn", feedback_id=feedback_id)
        return {"status": WITHDRAWN, "report_changed": False, "report": report}
    if decision == "dispute":
        _set_status(driver, feedback_id, DISPUTED)
        emit("feedback_disputed", feedback_id=feedback_id)
        return {"status": DISPUTED, "report_changed": False, "report": report}
    if decision == "rebut":
        # 새 라운드 — 발행자 재반박 코멘트로 재평가 (보고서 불변)
        new_comment = rebut_comment or comment
        _, target_claim = _resolve_target_claim(report, target_claim_id) or (None, None)
        reviser = Reviser()
        assessment, evidence = reviser.assess(
            driver, report, asset_id, target_claim, kind, new_comment)
        rnd = _next_round(driver, feedback_id)
        _write_assessment(driver, feedback_id, assessment, evidence, round_no=rnd)
        _set_status(driver, feedback_id, ASSESSED, stance=assessment.stance)
        emit("feedback_rebutted", feedback_id=feedback_id, round=rnd,
             stance=assessment.stance)
        return {"status": ASSESSED, "report_changed": False, "round": rnd,
                "stance": assessment.stance, "rationale": assessment.rationale,
                "report": report}
    if decision != "accept":
        raise ValueError(f"decision은 accept/withdraw/dispute/rebut — {decision}")
    return _apply_accept(driver, report, asset_id, feedback_id=feedback_id,
                         target_claim_id=target_claim_id, kind=kind, comment=comment,
                         out_dir=out_dir, asset_card=asset_card)
