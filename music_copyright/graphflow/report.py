"""보고서 생성 파이프라인 (음악저작권) — COMPOSE→WRITE→SYNTH→AUDIT + 산출. real_estate 미러.

산출물 3종:
    1. 구조화 JSON — section/block/claim ID + evidence 맵 (근거 원문/출처/발견경로)
    2. markdown 렌더 (사람 열람용)
    3. Report 서브그래프 — Report/ReportSection/Claim 노드 + HAS_SECTION/HAS_CLAIM/CITES

오케스트레이션은 순차 함수(generate_report). 기계층 위반 시 Writer 재작성. LLM 감사층·
Reviser·EXPAND는 후속(# TODO). LLM은 전부 GPT 티어(music_writer/synthesizer).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from neo4j import Driver

from music_copyright.graphflow.auditor import audit_overview, audit_section
from music_copyright.graphflow.axes import Axis, BASE_AXES, axis_by_key
from music_copyright.graphflow.composer import Evidence, compose
from music_copyright.graphflow.graph.queries import work_card_text
from music_copyright.graphflow.graph.schema import EDGE_TYPES as E
from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.graphflow.seeder import ScopeGraph, SeedEdge, seed, write_edges
from music_copyright.graphflow.synthesizer import MusicSynthesizer
from music_copyright.graphflow.writer import MusicWriter, SectionDraft

logger = logging.getLogger("music_copyright.report")

_MAX_REWRITES = 2


def _claim_id(axis: str, text: str) -> str:
    return "clm_" + hashlib.sha256(f"{axis}|{text}".encode()).hexdigest()[:10]


# work_card_text는 graph/queries.py로 이전(real_estate asset_card와 평행) — 위에서 import.


# ───────────────────────────────────────────────────────────
# 파이프라인 단계 (1단계 = 1함수) — workflow.py 노드가 호출
# ───────────────────────────────────────────────────────────
def write_axis(
    driver: Driver,
    work_id: str,
    work_card: str,
    axis: str | Axis,
    *,
    evidence_k: int | None = None,
    outline: list[str] | None = None,
) -> tuple[SectionDraft, list[Evidence], list[str]]:
    """축 1개: compose → write → 기계 검증 → 위반 시 재작성. 반환: (draft, evidence, 잔여위반).

    axis는 Axis(동적 축) 또는 키 문자열(기본 3축). 동적 축이면 intent/title/focus를
    compose·writer에 그대로 전달한다.
    """
    ax = axis if isinstance(axis, Axis) else (axis_by_key(axis) or Axis(axis, axis, ""))
    evidence = compose(driver, work_id, ax.key, k=evidence_k,
                       intent=ax.intent, news_intent=ax.news, profile=ax.prof)
    writer = MusicWriter()
    draft = writer.write(ax.key, work_card, evidence, outline=outline,
                         title=ax.title, focus=ax.writer_focus)
    violations = audit_section(draft, evidence)
    rewrites = 0
    while violations and rewrites < _MAX_REWRITES:
        logger.info("audit fail (%s): %s", ax.key, violations)
        draft = writer.write(ax.key, work_card, evidence, violations=violations,
                             outline=outline, title=ax.title, focus=ax.writer_focus)
        violations = audit_section(draft, evidence)
        rewrites += 1
    return draft, evidence, violations


def synthesize_overview(work_card: str, sections: dict[str, SectionDraft]):
    """3축 → Overview + 기계 검증 (위반 시 재합성 1회). 반환: (overview, index_map, 잔여위반)."""
    synth = MusicSynthesizer()
    overview, index_map = synth.synthesize(work_card, sections)
    violations = audit_overview(overview, len(index_map))
    if violations:
        logger.info("overview audit fail: %s", violations)
        overview, index_map = synth.synthesize(work_card, sections)
        violations = audit_overview(overview, len(index_map))
    return overview, index_map, violations


# ───────────────────────────────────────────────────────────
# 조립 — 구조화 JSON
# ───────────────────────────────────────────────────────────
def _assemble(report_id, work_id, sections, overview, index_map,
              evidence_by_axis, audit) -> dict:
    claim_ids: dict[tuple[str, int], str] = {}
    claims_json: dict[str, dict] = {}
    evidence_map: dict[str, dict] = {}
    sections_json = []

    for axis, sec in sections.items():
        ev_by_index = {ev.index: ev for ev in evidence_by_axis[axis]}
        for i, cl in enumerate(sec.claims):
            claim_ids[(axis, i)] = _claim_id(axis, cl.text)
        blocks_json = []
        claim_block: dict[int, str] = {}
        for bi, b in enumerate(sec.blocks):
            block_id = f"blk_{axis}_{bi}"
            blocks_json.append({
                "block_id": block_id, "markdown": b.markdown,
                "claim_ids": [claim_ids[(axis, i)] for i in b.claim_indexes
                              if (axis, i) in claim_ids],
            })
            for i in b.claim_indexes:
                claim_block.setdefault(i, block_id)
        for i, cl in enumerate(sec.claims):
            cites_refs = []
            for e_idx in cl.cites:
                ev = ev_by_index.get(e_idx)
                if ev is None:
                    continue
                cites_refs.append(ev.ref)
                evidence_map.setdefault(ev.ref, ev.to_map_entry())
            claims_json[claim_ids[(axis, i)]] = {
                "text": cl.text, "kind": cl.kind, "axis": axis,
                "cites": cites_refs,
                "derived_from": [claim_ids[(axis, j)] for j in cl.derived_from
                                 if (axis, j) in claim_ids],
                "computations": [c.model_dump() for c in getattr(cl, "computations", [])],
                "as_of": cl.as_of, "block_id": claim_block.get(i),
            }
        sections_json.append({
            "section_id": f"sec_{axis}", "axis": axis, "title": sec.title,
            "blocks": blocks_json,
        })

    # overview — C# 글로벌 인덱스 → 섹션 claim_id
    ov_blocks, ov_claim_ids = [], []
    for cl in overview.claims:
        cid = _claim_id("overview", cl.text)
        ov_claim_ids.append(cid)
        claims_json[cid] = {
            "text": cl.text, "kind": cl.kind, "axis": "overview",
            "cites": [claim_ids[index_map[c - 1]] for c in cl.cites
                      if 1 <= c <= len(index_map)],
            "derived_from": [], "computations": [], "as_of": cl.as_of, "block_id": None,
        }
    for bi, b in enumerate(overview.blocks):
        block_id = f"blk_overview_{bi}"
        ids = [ov_claim_ids[i] for i in b.claim_indexes if 0 <= i < len(ov_claim_ids)]
        ov_blocks.append({"block_id": block_id, "markdown": b.markdown, "claim_ids": ids})
        for cid in ids:
            if claims_json[cid]["block_id"] is None:
                claims_json[cid]["block_id"] = block_id
    sections_json.insert(0, {
        "section_id": "sec_overview", "axis": "overview",
        "title": overview.title, "blocks": ov_blocks,
    })

    return {
        "report_id": report_id, "version_id": "v1", "work_id": work_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "sections": sections_json, "claims": claims_json,
        "evidence": evidence_map, "audit": audit,
        "valuation": {
            "overall_grade": getattr(overview, "overall_grade", "") or "",
            "overall_rationale": getattr(overview, "overall_rationale", "") or "",
            "axis_scores": [
                {"axis": a.axis, "grade": a.grade, "rationale": a.rationale}
                for a in (getattr(overview, "axis_scores", None) or [])
            ],
        },
    }


def render_markdown(report: dict) -> str:
    """보고서 dict → markdown."""
    lines = ["# 음악저작물 사회·문화 가치 평가 보고서", "",
             f"- report: `{report['report_id']}` ({report['version_id']})",
             f"- work: `{report['work_id']}`",
             f"- 생성: {report['created_at']}", ""]
    val = report.get("valuation") or {}
    if val.get("overall_grade") or val.get("axis_scores"):
        lines.append(f"## 사회·문화 가치 종합 등급: **{val.get('overall_grade') or '-'}**\n")
        if val.get("overall_rationale"):
            lines += [val["overall_rationale"], ""]
        for a in val.get("axis_scores", []):
            lines.append(f"- **{a['axis']}** ({a['grade']}): {a['rationale']}")
        lines.append("")
    for sec in report["sections"]:
        lines.append(f"## [{sec['axis']}] {sec['title']}\n")
        for b in sec["blocks"]:
            lines += [b["markdown"], ""]
    lines.append("---\n\n## 인용 근거 (evidence)\n")
    for ref, ev in report["evidence"].items():
        origin = ev.get("origin") or {}
        src = origin.get("source") or ev.get("source_kind")
        url = f" — [{origin['url']}]({origin['url']})" if origin.get("url") else ""
        lines.append(f"- **{ref}** ({src}): {ev['text']}{url}")
    return "\n".join(lines)


# ───────────────────────────────────────────────────────────
# Report 서브그래프 적재
# ───────────────────────────────────────────────────────────
def write_report_graph(driver: Driver, report: dict) -> None:
    sg = ScopeGraph()
    edges: list[SeedEdge] = []
    rid = report["report_id"]
    sg.add_node(L.REPORT, rid, {
        "work_id": report["work_id"], "version_id": report["version_id"],
        "created_at": report["created_at"],
    })
    edges.append(SeedEdge(L.REPORT, rid, E.ABOUT, L.WORK, report["work_id"]))

    for sec in report["sections"]:
        sg.add_node(L.REPORT_SECTION, sec["section_id"],
                    {"axis": sec["axis"], "title": sec["title"], "report_id": rid})
        edges.append(SeedEdge(L.REPORT, rid, E.HAS_SECTION, L.REPORT_SECTION, sec["section_id"]))

    for cid, cl in report["claims"].items():
        sg.add_node(L.CLAIM, cid, {
            "text": cl["text"], "kind": cl["kind"], "axis": cl["axis"],
            "as_of": cl["as_of"], "block_id": cl["block_id"], "report_id": rid,
        })
        edges.append(SeedEdge(L.REPORT_SECTION, f"sec_{cl['axis']}",
                              E.HAS_CLAIM, L.CLAIM, cid))
        for ref in cl["cites"]:
            if ":" in ref:                       # evidence ref "Label:key"
                label, key = ref.split(":", 1)
            else:                                # overview → 섹션 claim id
                label, key = L.CLAIM, ref
            edges.append(SeedEdge(L.CLAIM, cid, E.CITES, label, key))

    seed(sg, driver)
    write_edges(driver, edges)


# ───────────────────────────────────────────────────────────
# finalize + 순차 진입점
# ───────────────────────────────────────────────────────────
def finalize_report(
    driver: Driver,
    work_id: str,
    sections: dict[str, SectionDraft],
    overview,
    index_map: list,
    evidence_by_axis: dict[str, list[Evidence]],
    mech_audit: dict[str, list[str]],
    *,
    out_dir: str | Path,
) -> dict:
    """조립 → 파일 저장 → Report 서브그래프 적재."""
    created = datetime.now().isoformat(timespec="seconds")
    report_id = "rpt_" + hashlib.sha256(f"{work_id}|{created}".encode()).hexdigest()[:10]
    report = _assemble(report_id, work_id, sections, overview, index_map,
                       evidence_by_axis, audit={"mechanical": mech_audit})

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(render_markdown(report), encoding="utf-8")

    try:
        write_report_graph(driver, report)
    except Exception as exc:  # noqa: BLE001 — 그래프 적재 실패가 파일 산출을 막지 않게
        logger.warning("Report 서브그래프 적재 실패(파일은 저장됨): %s", exc)

    total_mech = sum(len(v) for v in mech_audit.values())
    logger.info("report %s 완료 → %s (sections=%d claims=%d evidence=%d mech_viol=%d)",
                report_id, out, len(report["sections"]), len(report["claims"]),
                len(report["evidence"]), total_mech)
    return report


def generate_report(
    driver: Driver,
    work_id: str,
    *,
    out_dir: str | Path,
    evidence_k: int | None = None,
    expand_rounds: int = 0,
    axes: list[Axis] | None = None,
) -> dict:
    """순차 단독 실행 진입점 (LangGraph 없이). 표준 그래프 경로는 workflow.py.

    expand_rounds>0이면 집필 전 웹검색 EXPAND로 Fact를 보강한다(보고서가 두꺼워짐).
    axes 미지정이면 기본 3축. 동적 축(사람·에이전트)은 호출부가 resolve해 넘긴다.
    """
    axes = axes or BASE_AXES
    if expand_rounds > 0:
        from infra.agent.tracing import make_run_id
        from music_copyright.graphflow.expand import run_expand
        run_expand(driver, work_id=work_id, collected_by=make_run_id("exp"),
                   rounds=expand_rounds)
    card = work_card_text(driver, work_id)
    sections: dict[str, SectionDraft] = {}
    evidence_by_axis: dict[str, list[Evidence]] = {}
    mech_audit: dict[str, list[str]] = {}
    for ax in axes:
        draft, evidence, violations = write_axis(
            driver, work_id, card, ax, evidence_k=evidence_k)
        sections[ax.key] = draft
        evidence_by_axis[ax.key] = evidence
        mech_audit[ax.key] = violations
    overview, index_map, ov_violations = synthesize_overview(card, sections)
    mech_audit["overview"] = ov_violations
    return finalize_report(driver, work_id, sections, overview, index_map,
                           evidence_by_axis, mech_audit, out_dir=out_dir)
