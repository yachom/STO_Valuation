"""Reviser — 발행자 피드백 검수·수정 에이전트 (설계 §10.4/§10.5, D13/D14).

**에이전트는 피드백을 맹신하지 않는다.** assess()는 피드백 주장을 그래프 증거와
대조해 입장(agree/disagree/partial/inconclusive)을 인용과 함께 회신하고,
apply_edit/challenge/enrich는 수용된 피드백을 기존 인용 증거 범위 안에서만 외과적
으로 반영한다(새 사실 생성 금지).

상태머신·그래프 적재·이벤트 라이프사이클(submit_feedback/resolve_feedback)은
feedback.py가 이 에이전트를 호출해 구동한다 — 합의(agreed) 전에는 보고서 불변.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from real_estate.graphflow.agents.base import BaseAgent, korean_system_prompt, short_hash
from real_estate.graphflow.prompts import get_prompt
from real_estate.graphflow.composer import format_evidence, paths_to_evidence
from real_estate.graphflow.traversal import llm_walk, vector_anchors
from real_estate.graphflow.writer import ClaimDraft

logger = logging.getLogger("graphflow.reviser")


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
def _block_of(report: dict, claim_id: str) -> tuple[dict, dict] | None:
    """claim_id → (section dict, block dict)."""
    block_id = report["claims"][claim_id].get("block_id")
    for sec in report["sections"]:
        for b in sec["blocks"]:
            if b["block_id"] == block_id:
                return sec, b
    return None


def _claim_id(axis: str, text: str) -> str:
    """axis|text 안정 해시 → claim_id. edit/enrich 양쪽이 같은 규칙을 써야 한다."""
    return "clm_" + short_hash(f"{axis}|{text}", 10)


def _resolve_cites(cites, ev_by_index, report) -> list[str]:
    """[E#] 인용 인덱스 → evidence ref 리스트. 인용된 evidence를 report 맵에 등록.

    apply_edit(_splice_block)·apply_enrich(_append_block)가 공유 — 두 곳의
    인용 해소 로직이 갈라지지 않도록 한 곳에 둔다.
    """
    refs = []
    for e in cites:
        ev = ev_by_index.get(e)
        if ev is not None:
            refs.append(ev.ref)
            report["evidence"].setdefault(ev.ref, ev.to_map_entry())
    return refs


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
        # 옛 claim 제거
        for cid in old_claim_ids:
            report["claims"].pop(cid, None)
        # 새 claim 적재
        new_ids = []
        for cl in edited.claims:
            cid = _claim_id(sec["axis"], cl.text)
            cites_refs = _resolve_cites(cl.cites, ev_by_index, report)
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
        axis = section["axis"]
        n = len(section["blocks"])
        block_id = f"blk_{axis}_{n}"
        new_ids = []
        for cl in edited.claims:
            cid = _claim_id(axis, cl.text)
            cites_refs = _resolve_cites(cl.cites, ev_by_index, report)
            report["claims"][cid] = {
                "text": cl.text, "kind": cl.kind, "axis": axis,
                "cites": cites_refs, "derived_from": [], "as_of": cl.as_of,
                "block_id": block_id}
            new_ids.append(cid)
        section["blocks"].append({"block_id": block_id, "markdown": edited.markdown,
                                  "claim_ids": new_ids})
        report["_last_target_claim_id"] = new_ids[0] if new_ids else None
        return report
