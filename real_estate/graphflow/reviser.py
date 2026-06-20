"""Reviser — 발행자 피드백 합의 루프 (설계 §10.4/§10.5, D13/D14).

핵심 불변식: **에이전트는 피드백을 맹신하지 않고, 합의(agreed) 전에는 보고서가
절대 변하지 않는다.** 피드백이 오면 먼저 그래프 증거와 대조한 Assessment(인용
의무)를 회신하고, 발행자가 수용해야 비로소 외과적 수정 → 새 버전.

상태머신 (Feedback.status — 그래프가 체크포인트):
    open → assessed → ┬ agreed → applied   (수용 → 수정 → 새 버전)
                      ├ disputed            (재반박 누적, 미합의 보류 = 보고서 불변)
                      └ withdrawn           (철회 = 보고서 불변)

이벤트 구동 진입점 (보고서 생성 워크플로우와 별개 라이프사이클):
    submit_feedback(...)  → Feedback+Assessment 적재, 평가 회신
    resolve_feedback(...) → accept/rebut/withdraw 처리

그래프: (:Feedback)-[:TARGETS]->(Claim|Section), (:Assessment)-[:REPLIES_TO]->
(:Feedback), (:Assessment)-[:CITES]->(증거). 스레드로 영구 보존.

PR-F v1 범위: 합의 프로토콜 + edit kind. challenge/enrich/첨부(UserDoc)는 후속.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal
from uuid import uuid4

from neo4j import Driver
from pydantic import BaseModel, Field

from infra.agent.tracing import emit, node_scope
from real_estate.graphflow.agents.base import BaseAgent, korean_system_prompt
from real_estate.graphflow.prompts import get_prompt
from real_estate.graphflow.composer import format_evidence, paths_to_evidence
from real_estate.graphflow.graph.schema import EDGE_TYPES as E
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.report import (
    bump_version,
    rewrite_report_graph,
    save_report_files,
)
from real_estate.graphflow.seeder import ScopeGraph, SeedEdge, _write_edges, seed
from real_estate.graphflow.traversal import llm_walk, vector_anchors
from real_estate.graphflow.writer import ClaimDraft

logger = logging.getLogger("graphflow.reviser")

# 상태
OPEN, ASSESSED, AGREED, APPLIED, DISPUTED, WITHDRAWN = (
    "open", "assessed", "agreed", "applied", "disputed", "withdrawn")

_VALID_KINDS = {"edit", "challenge", "enrich"}


# ───────────────────────────────────────────────────────────
# 스키마
# ───────────────────────────────────────────────────────────
class AssessmentOutput(BaseModel):
    stance: Literal["agree", "disagree", "partial", "inconclusive"] = Field(
        description="피드백 주장에 대한 입장. agree=타당 / disagree=증거가 반박 / "
                    "partial=일부만 타당 / inconclusive=판단할 증거 부족")
    rationale: str = Field(description="그래프 증거에 근거한 판단 이유 (2~4문장)")
    cited_evidence_indexes: list[int] = Field(
        default_factory=list, description="판단 근거로 인용한 증거 [E#] 번호들")
    proposal: str | None = Field(
        default=None, description="반영(agreed) 시 어떻게 수정할지 한두 문장. disagree면 null")


class _EditedBlock(BaseModel):
    markdown: str = Field(description="재작성된 문단 markdown")
    claims: list[ClaimDraft] = Field(description="이 문단의 주장들 (cites는 기존 E# 유지/축소만)")


# ───────────────────────────────────────────────────────────
# 그래프 헬퍼
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


def _block_of(report: dict, claim_id: str) -> tuple[dict, dict] | None:
    """claim_id → (section dict, block dict)."""
    block_id = report["claims"][claim_id].get("block_id")
    for sec in report["sections"]:
        for b in sec["blocks"]:
            if b["block_id"] == block_id:
                return sec, b
    return None


# ───────────────────────────────────────────────────────────
# Reviser 에이전트
# ───────────────────────────────────────────────────────────
class Reviser(BaseAgent):
    name = "reviser"

    # ── 평가 (맹신 금지 — 증거 대조) ──────────────────────────
    def assess(self, driver, report, asset_id, target_claim, kind, comment, *,
               extra_evidence=None, max_hops=3):
        """피드백 주장 vs 그래프 증거 대조 → Assessment + 사용한 evidence.

        extra_evidence: 첨부 자료(UserDoc)에서 갓 추출한 증거 — 벡터 검색 우연에
        맡기지 않고 명시적으로 앞에 합쳐 대조한다 (§10.5 '신규 추출 Fact 대조').
        """
        evidence = self._gather_evidence(driver, asset_id, target_claim, comment,
                                         max_hops=max_hops)
        if extra_evidence:
            seen = {e.ref for e in extra_evidence}
            evidence = list(extra_evidence) + [e for e in evidence if e.ref not in seen]
            for i, e in enumerate(evidence, 1):    # re-index [E#]
                e.index = i
        cited = "\n".join(
            f"- {ref}" for ref in target_claim.get("cites", [])) or "(없음)"
        system = (korean_system_prompt(get_prompt("reviser.assess_role"))
                  + get_prompt("reviser.assess_rules"))
        user = (
            f"[현재 주장]\n{target_claim['text']}\n"
            f"종류: {target_claim['kind']}, 현 인용: {cited}\n\n"
            f"[발행자 피드백]\n종류: {kind}\n코멘트: {comment}\n\n"
            f"[관련 그래프 증거]\n{format_evidence(evidence)}\n\n"
            f"이 피드백이 타당한지 증거로 판단하고, 반영 시 수정 방향을 제시하세요."
        )
        out: AssessmentOutput = self.complete(system=system, user=user,
                                              schema=AssessmentOutput)
        return out, evidence

    def _gather_evidence(self, driver, asset_id, target_claim, comment, *, max_hops):
        anchor_eids = []
        for q in (comment, target_claim["text"]):
            for eid in vector_anchors(driver, q, k=3, scope_asset_id=asset_id,
                                      scope_hops=max_hops):
                if eid not in anchor_eids:
                    anchor_eids.append(eid)
        if not anchor_eids:
            return []
        paths = llm_walk(driver, anchor_eids[:5], comment, max_hops=max_hops, k=10)
        return paths_to_evidence(driver, paths)

    # ── 수정: edit (타깃 block 재작성, 타깃 외 불변) ──────────
    def apply_edit(self, report, asset_card, target_claim_id, comment, evidence):
        loc = _block_of(report, target_claim_id)
        if loc is None:
            raise ValueError(f"block 미발견: {target_claim_id}")
        sec, block = loc
        block_claims = [cid for cid in block["claim_ids"]]
        cur_claims = [report["claims"][cid] for cid in block_claims]
        ev_by_index = {ev.index: ev for ev in evidence}
        ev_lines = "\n".join(
            f"[E{ev.index}] {ev.ref}: {ev.text}" for ev in evidence)
        cur_lines = "\n".join(
            f"- ({c['kind']}) {c['text']} [cites: {', '.join(c['cites'])}]"
            for c in cur_claims)
        system = (korean_system_prompt(get_prompt("reviser.edit_role"))
                  + get_prompt("reviser.edit_rules"))
        user = (
            f"[자산]\n{asset_card}\n\n[현재 문단]\n{block['markdown']}\n\n"
            f"[현재 주장들]\n{cur_lines}\n\n[발행자 피드백]\n{comment}\n\n"
            f"[인용 가능 증거]\n{ev_lines}\n\n위 피드백을 반영해 문단을 재작성하세요."
        )
        out: _EditedBlock = self.complete(system=system, user=user, schema=_EditedBlock)
        return self._splice_block(report, sec, block, block_claims, out, ev_by_index)

    @staticmethod
    def _splice_block(report, sec, block, old_claim_ids, edited, ev_by_index):
        """재작성된 block을 report dict에 외과적 반영 — 타깃 block만 교체."""
        import hashlib

        def cid_of(text):
            return "clm_" + hashlib.sha256(f"{sec['axis']}|{text}".encode()).hexdigest()[:10]

        # 옛 claim 제거
        for cid in old_claim_ids:
            report["claims"].pop(cid, None)
        # 새 claim 적재
        new_ids = []
        for cl in edited.claims:
            cid = cid_of(cl.text)
            cites_refs = []
            for e in cl.cites:
                ev = ev_by_index.get(e)
                if ev is not None:
                    cites_refs.append(ev.ref)
                    report["evidence"].setdefault(ev.ref, ev.to_map_entry())
            report["claims"][cid] = {
                "text": cl.text, "kind": cl.kind, "axis": sec["axis"],
                "cites": cites_refs, "derived_from": [], "as_of": cl.as_of,
                "block_id": block["block_id"],
            }
            new_ids.append(cid)
        block["markdown"] = edited.markdown
        block["claim_ids"] = new_ids
        report["_last_target_claim_id"] = new_ids[0] if new_ids else None
        return report

    # ── 수정: challenge (증거 재판정 + 미니 EXPAND + 재작성) ──
    def apply_challenge(self, driver, report, asset_id, target_claim_id, comment,
                        asset_card, *, collected_by):
        """사실 이의 — 미니 EXPAND로 최신 증거 수집 후 해당 block 재작성/완화."""
        from real_estate.graphflow.expand import run_expand
        run_expand(driver, asset_id=asset_id, collected_by=collected_by,
                   purpose=comment, rounds=1, frontier_max=2, total_query_budget=3)
        claim = report["claims"][target_claim_id]
        evidence = self._gather_evidence(driver, asset_id, claim, comment, max_hops=3)
        directive = (f"[사실 이의] {comment}\n최신 증거를 반영해 주장을 갱신하세요. "
                     f"증거가 약하거나 만료(DECAYED/EXPIRED)면 단정을 완화하거나 "
                     f"'~로 보고된 바 있다' 수준으로 낮추고, 그래도 근거가 없으면 철회하세요.")
        return self.apply_edit(report, asset_card, target_claim_id, directive, evidence)

    # ── 수정: enrich (미니 EXPAND + 섹션 증보, 기존 block 불변) ──
    def apply_enrich(self, driver, report, asset_id, target_claim_id, comment,
                     asset_card, *, collected_by):
        """보강 요청 — 미니 EXPAND 후 타깃 섹션에 새 block 1개 추가 (기존 불변)."""
        from real_estate.graphflow.expand import run_expand
        run_expand(driver, asset_id=asset_id, collected_by=collected_by,
                   purpose=comment, rounds=1, frontier_max=2, total_query_budget=3)
        axis = report["claims"][target_claim_id]["axis"]
        section = next((s for s in report["sections"] if s["axis"] == axis), None)
        if section is None:
            raise ValueError(f"섹션 미발견: {axis}")
        evidence = self._gather_evidence(
            driver, asset_id, report["claims"][target_claim_id], comment, max_hops=3)
        ev_lines = "\n".join(f"[E{ev.index}] {ev.ref}: {ev.text}" for ev in evidence)
        system = (korean_system_prompt(get_prompt("reviser.augment_role", axis=axis))
                  + get_prompt("reviser.augment_rules"))
        user = (f"[자산]\n{asset_card}\n\n[보강 요청]\n{comment}\n\n"
                f"[새로 수집·관련 증거]\n{ev_lines}")
        out: _EditedBlock = self.complete(system=system, user=user, schema=_EditedBlock)
        if not out.claims:
            report["_last_target_claim_id"] = target_claim_id
            return report
        return self._append_block(report, section, out, {e.index: e for e in evidence})

    @staticmethod
    def _append_block(report, section, edited, ev_by_index):
        """섹션에 새 block 추가 — 기존 block 전부 불변."""
        import hashlib
        axis = section["axis"]
        n = len(section["blocks"])
        block_id = f"blk_{axis}_{n}"
        new_ids = []
        for cl in edited.claims:
            cid = "clm_" + hashlib.sha256(f"{axis}|{cl.text}".encode()).hexdigest()[:10]
            cites_refs = []
            for e in cl.cites:
                ev = ev_by_index.get(e)
                if ev is not None:
                    cites_refs.append(ev.ref)
                    report["evidence"].setdefault(ev.ref, ev.to_map_entry())
            report["claims"][cid] = {
                "text": cl.text, "kind": cl.kind, "axis": axis,
                "cites": cites_refs, "derived_from": [], "as_of": cl.as_of,
                "block_id": block_id}
            new_ids.append(cid)
        section["blocks"].append({"block_id": block_id, "markdown": edited.markdown,
                                  "claim_ids": new_ids})
        report["_last_target_claim_id"] = new_ids[0] if new_ids else None
        return report


# ───────────────────────────────────────────────────────────
# 이벤트 진입점
# ───────────────────────────────────────────────────────────
def _collected_evidence(driver, collected_by) -> list:
    """이번 피드백에서 수집된(collected_by) Fact → Evidence (첨부 자료 대조용)."""
    from real_estate.graphflow.composer import paths_to_evidence
    from real_estate.graphflow.traversal import EvidencePath, _load_nodes
    with driver.session() as s:
        eids = [r["eid"] for r in s.run(
            f"MATCH (f:{L.FACT} {{collected_by: $cb}}) RETURN elementId(f) AS eid",
            cb=collected_by)]
    if not eids:
        return []
    nodes = _load_nodes(driver, eids)
    return paths_to_evidence(driver, [EvidencePath([n], [], 1.0) for n in nodes.values()])


def _write_feedback(driver, report_id, feedback_id, target_id, kind, comment):
    sg = ScopeGraph()
    sg.add_node(L.FEEDBACK, feedback_id, {
        "report_id": report_id, "target_id": target_id, "kind": kind,
        "comment": comment, "status": OPEN,
        "created_at": datetime.now().isoformat(timespec="seconds")})
    seed(sg, driver)
    _write_edges(driver, [
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
    _write_edges(driver, edges)
    return aid


def _set_status(driver, feedback_id, status, **extra):
    sets = "".join(f", f.{k} = ${k}" for k in extra)
    with driver.session() as s:
        s.run(f"MATCH (f:{L.FEEDBACK} {{feedback_id: $fid}}) SET f.status = $st{sets}",
              fid=feedback_id, st=status, **extra)


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

    # ── accept: agreed → 외과적 수정 → 새 버전 ──
    _set_status(driver, feedback_id, AGREED)
    with node_scope("reviser_apply", kind=kind):
        if asset_card is None:
            from real_estate.graphflow.report import _asset_card
            asset_card = _asset_card(driver, asset_id)
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
