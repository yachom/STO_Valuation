"""보고서 생성 파이프라인 — COMPOSE→WRITE→SYNTH→AUDIT + 산출 (설계 §7 Phase 3~4, §10).

산출물 3종:
    1. 구조화 JSON (§10.3, D12) — section/block/claim ID + evidence 맵
       (근거 원문/출처/날짜/confidence/유효성/발견경로 — 사람 검증용 자급 스냅샷)
    2. markdown 렌더 (사람 열람용 파생물)
    3. Report 서브그래프 — Report/ReportSection/Claim 노드 +
       HAS_SECTION/HAS_CLAIM/CITES 엣지 (보고서가 그래프의 1급 시민이 됨)

오케스트레이션은 순차 함수 — critic 재진입·REVISE 루프가 붙는 후속 PR에서
LangGraph로 승격한다. 기계층 위반 시 Writer 재작성 1회.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from neo4j import Driver

from real_estate.graphflow.auditor import AuditorLLM, audit_overview, audit_section
from real_estate.graphflow.composer import Evidence, compose
from real_estate.graphflow.graph.schema import EDGE_TYPES as E
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.profiles import REPORT_AXES
from real_estate.graphflow.seeder import ScopeGraph, SeedEdge, _write_edges, seed
from real_estate.graphflow.synthesizer import Synthesizer
from infra.agent.tracing import add_score, emit, make_run_id, node_scope
from real_estate.graphflow.writer import SectionDraft, Writer

logger = logging.getLogger("graphflow.report")

_MAX_REWRITES = 1


def _claim_id(axis: str, text: str) -> str:
    return "clm_" + hashlib.sha256(f"{axis}|{text}".encode()).hexdigest()[:10]


def _asset_card(driver: Driver, asset_id: str) -> str:
    with driver.session() as s:
        r = s.run(f"MATCH (a:{L.ASSET} {{asset_id: $aid}}) RETURN a.card_text AS c",
                  aid=asset_id).single()
        if r is None:
            raise ValueError(f"Asset 노드 없음: {asset_id}")
        return r["c"] or asset_id


# ───────────────────────────────────────────────────────────
# 조립 — 구조화 JSON (§10.3)
# ───────────────────────────────────────────────────────────
def _assemble(
    report_id: str,
    asset_id: str,
    sections: dict[str, SectionDraft],
    overview,                       # OverviewDraft
    index_map: list[tuple[str, int]],
    evidence_by_axis: dict[str, list[Evidence]],
    audit: dict,
) -> dict:
    claim_ids: dict[tuple[str, int], str] = {}      # (axis, claim_idx) → clm_id
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
                "as_of": cl.as_of, "block_id": claim_block.get(i),
            }
        sections_json.append({
            "section_id": f"sec_{axis}", "axis": axis, "title": sec.title,
            "blocks": blocks_json,
        })

    # overview — C# 글로벌 인덱스 → 섹션 claim_id
    ov_blocks = []
    ov_claim_ids = []
    for i, cl in enumerate(overview.claims):
        cid = _claim_id("overview", cl.text)
        ov_claim_ids.append(cid)
        claims_json[cid] = {
            "text": cl.text, "kind": cl.kind, "axis": "overview",
            "cites": [claim_ids[index_map[c - 1]] for c in cl.cites
                      if 1 <= c <= len(index_map)],
            "derived_from": [], "as_of": cl.as_of, "block_id": None,
        }
    for bi, b in enumerate(overview.blocks):
        block_id = f"blk_overview_{bi}"
        ids = [ov_claim_ids[i] for i in b.claim_indexes if 0 <= i < len(ov_claim_ids)]
        ov_blocks.append({"block_id": block_id, "markdown": b.markdown,
                          "claim_ids": ids})
        for cid in ids:
            if claims_json[cid]["block_id"] is None:
                claims_json[cid]["block_id"] = block_id
    sections_json.insert(0, {
        "section_id": "sec_overview", "axis": "overview",
        "title": overview.title, "blocks": ov_blocks,
    })

    return {
        "report_id": report_id, "version_id": "v1", "parent_version_id": None,
        "asset_id": asset_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "sections": sections_json, "claims": claims_json,
        "evidence": evidence_map, "audit": audit,
        # 사회적 가치 루브릭 (synthesizer 판정) — 묘사가 아닌 등급 결론.
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
    lines = [f"# 상가 자산 정성 평가 보고서", f"",
             f"- report: `{report['report_id']}` ({report['version_id']})",
             f"- asset: `{report['asset_id']}`",
             f"- 생성: {report['created_at']}", ""]
    val = report.get("valuation") or {}
    if val.get("overall_grade") or val.get("axis_scores"):
        lines.append(f"## 사회적 가치 종합 등급: **{val.get('overall_grade') or '-'}**\n")
        if val.get("overall_rationale"):
            lines.append(val["overall_rationale"])
            lines.append("")
        for a in val.get("axis_scores", []):
            lines.append(f"- **{a['axis']}** ({a['grade']}): {a['rationale']}")
        lines.append("")
    for sec in report["sections"]:
        lines.append(f"## [{sec['axis']}] {sec['title']}\n")
        for b in sec["blocks"]:
            lines.append(b["markdown"])
            lines.append("")
    lines.append("---\n\n## 인용 근거 (evidence)\n")
    for ref, ev in report["evidence"].items():
        origin = ev.get("origin") or {}
        src = origin.get("press") or ev.get("source_kind")
        url = f" — {origin['url']}" if origin.get("url") else ""
        date = (ev.get("published_at") or "")[:10]
        vstat = ev["validity"]["status"]
        lines.append(f"- **{ref}** ({src}, {date}, {vstat}): {ev['text']}{url}")
    return "\n".join(lines)


# ───────────────────────────────────────────────────────────
# Report 서브그래프 적재
# ───────────────────────────────────────────────────────────
def write_report_graph(driver: Driver, report: dict) -> None:
    sg = ScopeGraph()
    edges: list[SeedEdge] = []
    rid = report["report_id"]
    sg.add_node(L.REPORT, rid, {
        "asset_id": report["asset_id"], "version_id": report["version_id"],
        "created_at": report["created_at"],
    })
    edges.append(SeedEdge(L.REPORT, rid, E.ABOUT, L.ASSET, report["asset_id"]))

    for sec in report["sections"]:
        sg.add_node(L.REPORT_SECTION, sec["section_id"],
                    {"axis": sec["axis"], "title": sec["title"], "report_id": rid})
        edges.append(SeedEdge(L.REPORT, rid, E.HAS_SECTION,
                              L.REPORT_SECTION, sec["section_id"]))

    for cid, cl in report["claims"].items():
        sg.add_node(L.CLAIM, cid, {
            "text": cl["text"], "kind": cl["kind"], "axis": cl["axis"],
            "as_of": cl["as_of"], "block_id": cl["block_id"], "report_id": rid,
        })
        edges.append(SeedEdge(L.REPORT_SECTION, f"sec_{cl['axis']}",
                              E.HAS_CLAIM, L.CLAIM, cid))
        for ref in cl["cites"]:
            if ":" in ref:                    # evidence ref "Label:key"
                label, key = ref.split(":", 1)
            else:                             # overview → 섹션 claim id ("clm_…")
                label, key = L.CLAIM, ref
            edges.append(SeedEdge(L.CLAIM, cid, E.CITES, label, key))
        for dcid in cl["derived_from"]:
            edges.append(SeedEdge(L.CLAIM, cid, E.CITES, L.CLAIM, dcid))

    seed(sg, driver)
    _write_edges(driver, edges)


def rewrite_report_graph(driver: Driver, report: dict) -> None:
    """Report 서브그래프를 외과적으로 교체 — 기존 Claim/Section 제거 후 재적재.

    REVISE에서 claim_id가 바뀌어도(텍스트 수정) 잔존 노드가 안 남게 한다.
    Report 노드 자체는 MERGE로 갱신.
    """
    rid = report["report_id"]
    with driver.session() as s:
        s.run(
            f"""
            MATCH (r:{L.REPORT} {{report_id: $rid}})-[:{E.HAS_SECTION}]->(sec)
            OPTIONAL MATCH (sec)-[:{E.HAS_CLAIM}]->(c:{L.CLAIM})
            DETACH DELETE sec, c
            """,
            rid=rid,
        )
    write_report_graph(driver, report)


def bump_version(report: dict, *, notes: str = "") -> dict:
    """버전 증가 + parent 체인. report dict를 in-place 갱신 후 반환."""
    cur = report.get("version_id", "v1")
    n = int(cur[1:]) if cur.startswith("v") and cur[1:].isdigit() else 1
    report["parent_version_id"] = cur
    report["version_id"] = f"v{n + 1}"
    report["created_at"] = datetime.now().isoformat(timespec="seconds")
    if notes:
        report.setdefault("revision_notes", []).append(notes)
    return report


def save_report_files(report: dict, out_dir: str | Path) -> Path:
    """버전 파일(report_{version}.json + .md) 저장. 최신은 report.json/md로도 미러."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ver = report["version_id"]
    md = render_markdown(report)
    (out / f"report_{ver}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / f"report_{ver}.md").write_text(md, encoding="utf-8")
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(md, encoding="utf-8")
    return out


# ───────────────────────────────────────────────────────────
# 파이프라인 단계 헬퍼 — LangGraph workflow.py의 노드들이 호출 (1단계=1함수)
# ───────────────────────────────────────────────────────────
def write_axis(
    driver: Driver,
    asset_id: str,
    asset_card: str,
    axis: str,
    *,
    evidence_k: int = 12,
) -> tuple[SectionDraft, list[Evidence], list[str]]:
    """축 1개: compose → write → 기계 검증 → 위반 시 재작성 1회.

    반환: (최종 draft, evidence, 잔여 위반 — 비면 통과).
    """
    with node_scope(f"write_{axis}"):
        evidence = compose(driver, asset_id, axis, k=evidence_k)
        writer = Writer()
        draft = writer.write(axis, asset_card, evidence)
        violations = audit_section(draft, evidence)
        rewrites = 0
        while violations and rewrites < _MAX_REWRITES:
            emit("audit_mechanical_fail", axis=axis, violations=violations)
            draft = writer.write(axis, asset_card, evidence, violations=violations)
            violations = audit_section(draft, evidence)
            rewrites += 1
        return draft, evidence, violations


def synthesize_overview(asset_card: str, sections: dict[str, SectionDraft]):
    """3축 → Overview + 기계 검증 (위반 시 재합성 1회).

    반환: (overview, index_map, 잔여 위반).
    """
    with node_scope("synthesize"):
        synth = Synthesizer()
        overview, index_map = synth.synthesize(asset_card, sections)
        violations = audit_overview(overview, len(index_map))
        if violations:
            emit("audit_mechanical_fail", axis="overview", violations=violations)
            overview, index_map = synth.synthesize(asset_card, sections)
            violations = audit_overview(overview, len(index_map))
        return overview, index_map, violations


def finalize_report(
    driver: Driver,
    asset_id: str,
    sections: dict[str, SectionDraft],
    overview,
    index_map: list,
    evidence_by_axis: dict[str, list[Evidence]],
    mech_audit: dict[str, list[str]],
    *,
    out_dir: str | Path,
) -> dict:
    """조립 → LLM층 감사 → 파일 저장 → Report 서브그래프 적재."""
    report_id = make_run_id("rpt")
    report = _assemble(report_id, asset_id, sections, overview, index_map,
                       evidence_by_axis, audit={"mechanical": mech_audit})
    md = render_markdown(report)

    with node_scope("audit_llm"):
        notes = AuditorLLM().review(md)
        report["audit"]["llm"] = notes.model_dump()

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(md, encoding="utf-8")

    write_report_graph(driver, report)
    total_mech = sum(len(v) for v in mech_audit.values())
    emit("report_done", report_id=report_id,
         sections=len(report["sections"]), claims=len(report["claims"]),
         evidence=len(report["evidence"]),
         mech_violations={k: len(v) for k, v in mech_audit.items()},
         llm_pass=report["audit"]["llm"]["overall_pass"])
    # Langfuse 품질 점수 (시간에 따른 보고서 품질 추적)
    add_score("audit_llm_pass", 1 if report["audit"]["llm"]["overall_pass"] else 0,
              data_type="BOOLEAN")
    add_score("audit_mech_violations", total_mech)
    add_score("claims_total", len(report["claims"]))
    logger.info("report %s 생성 완료 → %s", report_id, out)
    return report


def generate_report(
    driver: Driver,
    asset_id: str,
    *,
    out_dir: str | Path,
    evidence_k: int = 12,
) -> dict:
    """순차 단독 실행 진입점 (LangGraph 없이). 표준 경로는 workflow.py."""
    asset_card = _asset_card(driver, asset_id)
    sections: dict[str, SectionDraft] = {}
    evidence_by_axis: dict[str, list[Evidence]] = {}
    mech_audit: dict[str, list[str]] = {}
    for axis in REPORT_AXES:
        draft, evidence, violations = write_axis(
            driver, asset_id, asset_card, axis, evidence_k=evidence_k)
        sections[axis] = draft
        evidence_by_axis[axis] = evidence
        mech_audit[axis] = violations
    overview, index_map, ov_violations = synthesize_overview(asset_card, sections)
    mech_audit["overview"] = ov_violations
    return finalize_report(driver, asset_id, sections, overview, index_map,
                           evidence_by_axis, mech_audit, out_dir=out_dir)
