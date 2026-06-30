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
import re
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
_GAP_TERMS = (
    "부족", "확인되지", "확인되지 않는다", "확인되지 않았다", "확인하기 어렵",
    "판단하기 어렵", "제한적", "제한된다", "근거가 없다", "근거가 부족",
    "공백", "불충분", "not confirmed", "insufficient", "not enough evidence",
)
_GAP_AXIS_PRIORITY = {
    "social_value": 0,
    "influence": 1,
    "popularity": 2,
    "sustainability": 3,
}


def _claim_id(report_id: str, axis: str, text: str) -> str:
    return "clm_" + hashlib.sha256(
        f"{report_id}|{axis}|{text}".encode()).hexdigest()[:14]


def _section_id(report_id: str, axis: str) -> str:
    return "sec_" + hashlib.sha256(f"{report_id}|{axis}".encode()).hexdigest()[:14]


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
    expected_axes = [sec.title for sec in sections.values()]
    violations = audit_overview(overview, len(index_map), expected_axes)
    if violations:
        logger.info("overview audit fail: %s", violations)
        overview, index_map = synth.synthesize(work_card, sections)
        violations = audit_overview(overview, len(index_map), expected_axes)
    return overview, index_map, violations


def _work_id_type(work_id: str) -> str:
    wid = work_id or ""
    if wid.lower().startswith("isrc:"):
        return "ISRC"
    if re.fullmatch(r"T-\d{3}\.\d{3}\.\d{3}-\d", wid):
        return "ISWC"
    if ":" in wid:
        return wid.split(":", 1)[0].upper()
    return "internal"


def _work_identifier(work_id: str) -> str:
    if ":" in (work_id or ""):
        return work_id.split(":", 1)[1]
    return work_id


def _target_briefing(work_id: str, work_card: str) -> dict:
    """그래프 Work 카드에서 보고서 대상 브리핑을 기계적으로 만든다."""
    first = (work_card or "").splitlines()[0] if work_card else ""
    title = ""
    artist = ""
    if " — " in first:
        title, rest = first.split(" — ", 1)
        artist = re.split(r"\s*(?:는|은)\s*", rest, maxsplit=1)[0].strip()
    year_match = re.search(r"((?:19|20)\d{2})\s*발매", first)
    release_year = year_match.group(1) if year_match else ""
    identifier_type = _work_id_type(work_id)
    identifier = _work_identifier(work_id)
    if title and artist and release_year:
        briefing = f"{artist}의 {release_year}년 발매 음악저작물 '{title}'에 관한 평가 보고서"
    elif title and artist:
        briefing = f"{artist}의 음악저작물 '{title}'에 관한 평가 보고서"
    elif title:
        briefing = f"음악저작물 '{title}'에 관한 평가 보고서"
    else:
        briefing = f"음악저작물 {work_id}에 관한 평가 보고서"
    return {
        "title": title.strip(),
        "artist": artist,
        "release_year": release_year,
        "work_id": work_id,
        "work_id_type": identifier_type,
        "identifier": identifier,
        "briefing": briefing,
        "graph_card": work_card,
    }


def _is_gap_claim(text: str, kind: str | None = None) -> bool:
    if kind == "gap":
        return True
    t = (text or "").lower()
    return any(term.lower() in t for term in _GAP_TERMS)


def _gap_claim_texts(section: SectionDraft) -> list[str]:
    out: list[str] = []
    for cl in section.claims:
        if _is_gap_claim(cl.text, getattr(cl, "kind", None)):
            out.append(cl.text)
    return out


def _gap_expand_purpose(axis: Axis, gaps: list[str], work_card: str) -> str:
    focus = axis.writer_focus or axis.intent or ""
    bullets = "\n".join(f"- {g}" for g in gaps[:5])
    return (
        f"{work_card}\n\n"
        f"보고서 초안의 '{axis.title}' 축에서 아래 증거 공백이 확인됐다.\n"
        f"{bullets}\n\n"
        "평가 대상 곡명과 아티스트를 함께 앵커링해서 추가 근거를 찾고, "
        f"이 축의 공백을 직접 메울 수 있는 검증 가능한 자료를 우선 탐색하라. "
        f"축 초점: {focus}"
    )


def _select_gap_axes(
    sections: dict[str, SectionDraft],
    axis_by: dict[str, Axis],
    fact_counts: dict[str, int],
    *,
    min_axis_facts: int,
    max_axes: int,
) -> list[tuple[str, list[str]]]:
    """gap 보강 대상 축 선정 — writer의 '근거 부족' 문구 OR 축 Fact 수<임계(정량 공백).

    writer가 자연어 gap 문구를 남기지 않아도, 상업축은 Fact가 풍부하고 social_value만
    굶는 구조적 비대칭을 정량 게이트로 포착한다(min_axis_facts<=0이면 정량 게이트 끔).
    social_value가 최우선(_GAP_AXIS_PRIORITY)이라 max_axes 컷에서도 먼저 선택된다.
    """
    gap_axes: list[tuple[str, list[str]]] = []
    for key, sec in sections.items():
        if key not in axis_by:
            continue
        gaps = _gap_claim_texts(sec)
        thin = min_axis_facts > 0 and fact_counts.get(key, 0) < min_axis_facts
        if not gaps and thin:
            gaps = [f"축 Fact {fact_counts.get(key, 0)}건(<{min_axis_facts}) — 정량 증거 공백"]
        if gaps:
            gap_axes.append((key, gaps))
    gap_axes.sort(key=lambda item: (_GAP_AXIS_PRIORITY.get(item[0], 99),
                                    -len(item[1]), item[0]))
    return gap_axes[:max_axes]


def post_write_gap_feedback(
    driver: Driver,
    work_id: str,
    work_card: str,
    sections: dict[str, SectionDraft],
    evidence_by_axis: dict[str, list[Evidence]],
    mech_audit: dict[str, list[str]],
    *,
    axes: list[Axis],
    evidence_k: int | None = None,
    outline: dict | None = None,
) -> tuple[dict[str, SectionDraft], dict[str, list[Evidence]], dict[str, list[str]], dict]:
    """writer가 남긴 gap claim을 제어 신호로 삼아 1회 보강 탐색 후 해당 축만 재작성."""
    from infra.agent.tracing import emit, make_run_id
    from music_copyright.graphflow.config import get_config
    from music_copyright.graphflow.expand import run_expand

    cfg = get_config().get("qa", {}).get("post_write_gap_expand", {})
    if not cfg.get("enabled", True):
        summary = {"enabled": False, "skipped": True}
        emit("post_write_gap_feedback", **summary)
        return sections, evidence_by_axis, mech_audit, summary

    max_axes = int(cfg.get("max_axes", 2))
    min_axis_facts = int(cfg.get("min_axis_facts", 0))
    axis_overrides = cfg.get("axis_overrides", {}) or {}
    axis_by = {a.key: a for a in axes}

    # 정량 공백 게이트용 축별 Fact 수 (writer gap 문구가 없어도 굶는 축을 포착)
    fact_counts: dict[str, int] = {}
    if min_axis_facts > 0:
        try:
            from music_copyright.graphflow.coverage import axis_coverage
            fact_counts = {k: v.n_facts
                           for k, v in axis_coverage(driver, work_id).items()}
        except Exception as exc:  # noqa: BLE001 — 측정 실패가 보강을 막지 않게(문구 게이트만)
            logger.warning("gap 정량 게이트용 커버리지 측정 실패(문구 게이트만 사용): %s", exc)

    gap_axes = _select_gap_axes(
        sections, axis_by, fact_counts,
        min_axis_facts=min_axis_facts, max_axes=max_axes)
    if not gap_axes:
        summary = {"enabled": True, "skipped": True, "reason": "no_gap_claims"}
        emit("post_write_gap_feedback", **summary)
        return sections, evidence_by_axis, mech_audit, summary

    collected_by = make_run_id("gap")
    expand_summaries = []
    for key, gaps in gap_axes:
        ax = axis_by[key]
        # 축별 예산 분기 — social_value는 굶기 쉬워 rounds·query 예산을 키운다(config).
        ov = axis_overrides.get(key, {}) or {}
        try:
            expand_summaries.append({
                "axis": key,
                "summary": run_expand(
                    driver,
                    work_id=work_id,
                    collected_by=collected_by,
                    rounds=int(ov.get("rounds", cfg.get("rounds", 1))),
                    frontier_max=int(ov.get("frontier_max", cfg.get("frontier_max", 2))),
                    total_query_budget=int(ov.get(
                        "total_query_budget", cfg.get("total_query_budget", 3))),
                    purpose=_gap_expand_purpose(ax, gaps, work_card),
                ),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-write gap expand 실패(%s): %s", key, exc)
            expand_summaries.append({"axis": key, "error": type(exc).__name__})

    graph_refresh: dict = {}
    try:
        from music_copyright.graphflow.config import get_config as _cfg
        c = _cfg().get("consolidate", {})
        if c.get("enabled", True):
            from music_copyright.graphflow.consolidate import consolidate_entities
            graph_refresh["consolidate"] = consolidate_entities(
                driver, work_id,
                mode=c.get("mode", "rules_first"),
                min_conf=float(c.get("min_conf", 0.6)),
                scope_hops=int(c.get("scope_hops", 4)),
            )
        ag = _cfg().get("agentic_graph", {})
        if ag.get("enabled", False):
            from music_copyright.graphflow.agentic_graph import run_agentic_graph
            graph_refresh["agentic_graph"] = run_agentic_graph(
                driver, work_id, config=ag)
    except Exception as exc:  # noqa: BLE001
        graph_refresh["error"] = type(exc).__name__
        logger.warning("post-write gap graph refresh 실패(재작성은 계속): %s", exc)

    new_sections = dict(sections)
    new_evidence = dict(evidence_by_axis)
    new_audit = dict(mech_audit)
    rewritten: list[str] = []
    for key, _gaps in gap_axes:
        ax = axis_by[key]
        draft, evidence, violations = write_axis(
            driver,
            work_id,
            work_card,
            ax,
            evidence_k=evidence_k,
            outline=(outline or {}).get(key),
        )
        new_sections[key] = draft
        new_evidence[key] = evidence
        new_audit[key] = violations
        rewritten.append(key)

    summary = {
        "enabled": True,
        "axes": [key for key, _ in gap_axes],
        "rewritten": rewritten,
        "expand_summaries": expand_summaries,
        "graph_refresh": graph_refresh,
    }
    emit("post_write_gap_feedback", **summary)
    return new_sections, new_evidence, new_audit, summary


# ───────────────────────────────────────────────────────────
# 조립 — 구조화 JSON
# ───────────────────────────────────────────────────────────
def _assemble(report_id, work_id, work_card, created_at, sections, overview, index_map,
              evidence_by_axis, audit) -> dict:
    claim_ids: dict[tuple[str, int], str] = {}
    claims_json: dict[str, dict] = {}
    evidence_map: dict[str, dict] = {}
    sections_json = []
    section_ids = {axis: _section_id(report_id, axis) for axis in sections}
    section_ids["overview"] = _section_id(report_id, "overview")

    for axis, sec in sections.items():
        ev_by_index = {ev.index: ev for ev in evidence_by_axis[axis]}
        for i, cl in enumerate(sec.claims):
            claim_ids[(axis, i)] = _claim_id(report_id, axis, cl.text)
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
            "section_id": section_ids[axis], "axis": axis, "title": sec.title,
            "blocks": blocks_json,
        })

    # overview — C# 글로벌 인덱스 → 섹션 claim_id
    ov_blocks, ov_claim_ids = [], []
    for cl in overview.claims:
        cid = _claim_id(report_id, "overview", cl.text)
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
        "section_id": section_ids["overview"], "axis": "overview",
        "title": overview.title, "blocks": ov_blocks,
    })

    return {
        "report_id": report_id, "version_id": "v1", "work_id": work_id,
        "created_at": created_at,
        "target": _target_briefing(work_id, work_card),
        "sections": sections_json, "claims": claims_json,
        "evidence": evidence_map, "audit": audit,
        "valuation": {
            "asset_grade": getattr(overview, "asset_grade", "") or "",
            "asset_rationale": getattr(overview, "asset_rationale", "") or "",
            "social_grade": getattr(overview, "social_grade", "") or "",
            "social_rationale": getattr(overview, "social_rationale", "") or "",
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
    target = report.get("target") or {}
    lines = [
        "# 음악저작물 사회·문화 가치 평가 보고서",
        "",
        "## 기계 생성 메타데이터",
        "",
        f"- `report_id`: `{report['report_id']}`",
        f"- `version_id`: `{report['version_id']}`",
        f"- `work_id`: `{report['work_id']}`",
        f"- `work_id_type`: `{target.get('work_id_type') or _work_id_type(report['work_id'])}`",
        f"- `created_at`: `{report['created_at']}`",
        "",
        "## 평가 대상 브리핑",
        "",
        f"- `title`: `{target.get('title') or '-'}`",
        f"- `artist`: `{target.get('artist') or '-'}`",
        f"- `release_year`: `{target.get('release_year') or '-'}`",
        f"- `identifier`: `{target.get('identifier') or _work_identifier(report['work_id'])}`",
        f"- `briefing`: {target.get('briefing') or '-'}",
        "",
        "## LLM 작성 평가",
        "",
    ]
    val = report.get("valuation") or {}
    if val.get("overall_grade") or val.get("axis_scores") or val.get("social_grade"):
        lines.append("### 평가 종합\n")
        # 이중 트랙 — 자산가치(상업)와 사회적 가치를 나란히 (한쪽이 다른 쪽을 가리지 않게)
        if val.get("asset_grade") or val.get("social_grade"):
            lines.append(f"- **자산가치(상업) 등급: {val.get('asset_grade') or '-'}**"
                         + (f" — {val['asset_rationale']}" if val.get("asset_rationale") else ""))
            lines.append(f"- **사회적 가치 등급: {val.get('social_grade') or '-'}**"
                         + (f" — {val['social_rationale']}" if val.get("social_rationale") else ""))
            lines.append("")
        if val.get("overall_grade"):
            lines.append(f"#### 종합 등급: **{val.get('overall_grade') or '-'}**\n")
            if val.get("overall_rationale"):
                lines += [val["overall_rationale"], ""]
        if val.get("axis_scores"):
            lines.append("#### 축별 등급\n")
            for a in val.get("axis_scores", []):
                lines.append(f"- **{a['axis']}** ({a['grade']}): {a['rationale']}")
            lines.append("")
    for sec in report["sections"]:
        lines.append(f"### [{sec['axis']}] {sec['title']}\n")
        for b in sec["blocks"]:
            lines += [b["markdown"], ""]
            claim_ids = [cid for cid in b.get("claim_ids", []) if cid in report["claims"]]
            cite_refs = list(dict.fromkeys(
                ref for cid in claim_ids for ref in report["claims"][cid].get("cites", [])))
            if claim_ids:
                lines.append("_추적 claim_: " + ", ".join(f"`{cid}`" for cid in claim_ids))
            if cite_refs:
                label = "근거 claim" if sec["axis"] == "overview" else "근거 evidence"
                lines.append(f"_{label}_: " + ", ".join(f"`{ref}`" for ref in cite_refs))
            if claim_ids or cite_refs:
                lines.append("")
    lines.append("---\n\n## 기계 생성 인용 근거 (evidence)\n")
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
    target = report.get("target") or {}
    sg.add_node(L.REPORT, rid, {
        "work_id": report["work_id"], "version_id": report["version_id"],
        "created_at": report["created_at"],
        "target_title": target.get("title"),
        "target_artist": target.get("artist"),
        "work_id_type": target.get("work_id_type"),
    })
    edges.append(SeedEdge(L.REPORT, rid, E.ABOUT, L.WORK, report["work_id"]))

    section_by_axis = {sec["axis"]: sec["section_id"] for sec in report["sections"]}
    for sec in report["sections"]:
        sg.add_node(L.REPORT_SECTION, sec["section_id"],
                    {"axis": sec["axis"], "title": sec["title"], "report_id": rid})
        edges.append(SeedEdge(L.REPORT, rid, E.HAS_SECTION, L.REPORT_SECTION, sec["section_id"]))

    for cid, cl in report["claims"].items():
        sg.add_node(L.CLAIM, cid, {
            "text": cl["text"], "kind": cl["kind"], "axis": cl["axis"],
            "as_of": cl["as_of"], "block_id": cl["block_id"], "report_id": rid,
        })
        edges.append(SeedEdge(L.REPORT_SECTION, section_by_axis[cl["axis"]],
                              E.HAS_CLAIM, L.CLAIM, cid))
        for ref in cl["cites"]:
            if ref in report["claims"]:           # overview → 이 보고서의 섹션 Claim
                label, key = L.CLAIM, ref
            elif ":" in ref:                     # evidence ref "Label:key"
                label, key = ref.split(":", 1)
            else:
                logger.warning("알 수 없는 citation ref 생략: %s", ref)
                continue
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
    gap_feedback_summary: dict | None = None,
    pipeline_summary: dict | None = None,
) -> dict:
    """조립 → 파일 저장 → Report 서브그래프 적재."""
    created = datetime.now().isoformat(timespec="seconds")
    report_id = "rpt_" + hashlib.sha256(f"{work_id}|{created}".encode()).hexdigest()[:10]
    audit = {"mechanical": mech_audit}
    if gap_feedback_summary:
        audit["gap_feedback"] = gap_feedback_summary
    if pipeline_summary:
        audit["pipeline"] = pipeline_summary
    audit["retrieval"] = {
        axis: {
            "evidence_count": len(items),
            "source_kinds": dict(sorted({
                kind: sum(1 for ev in items if ev.source_kind == kind)
                for kind in {ev.source_kind for ev in items}
            }.items())),
            "independent_sources": len({
                (ev.origin or {}).get("url") or (ev.origin or {}).get("source")
                for ev in items
                if (ev.origin or {}).get("url") or (ev.origin or {}).get("source")
            }),
        }
        for axis, items in evidence_by_axis.items()
    }
    report = _assemble(report_id, work_id, work_card_text(driver, work_id), created,
                       sections, overview, index_map, evidence_by_axis, audit=audit)

    graph_audit = {"status": "written"}
    try:
        write_report_graph(driver, report)
    except Exception as exc:  # noqa: BLE001 — 그래프 적재 실패가 파일 산출을 막지 않게
        graph_audit = {"status": "failed", "error": type(exc).__name__}
        logger.warning("Report 서브그래프 적재 실패(파일 산출은 계속): %s", exc)
    report["audit"]["report_graph"] = graph_audit

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(render_markdown(report), encoding="utf-8")

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
    pipeline_summary: dict = {}
    if expand_rounds > 0:
        from infra.agent.tracing import make_run_id
        from music_copyright.graphflow.expand import run_expand
        pipeline_summary["collection"] = run_expand(
            driver, work_id=work_id, collected_by=make_run_id("exp"),
            rounds=expand_rounds)
    try:
        from music_copyright.graphflow.config import get_config
        cfg = get_config()
        c = cfg.get("consolidate", {})
        if c.get("enabled", True):
            from music_copyright.graphflow.consolidate import consolidate_entities
            pipeline_summary["consolidate"] = consolidate_entities(
                driver, work_id,
                mode=c.get("mode", "rules_first"),
                min_conf=float(c.get("min_conf", 0.6)),
                scope_hops=int(c.get("scope_hops", 4)),
            )
        ag = cfg.get("agentic_graph", {})
        if ag.get("enabled", False):
            from music_copyright.graphflow.agentic_graph import run_agentic_graph
            pipeline_summary["agentic_graph"] = run_agentic_graph(
                driver, work_id, config=ag)
    except Exception as exc:  # noqa: BLE001 — 승격 실패가 보고서 산출을 막지 않게
        logger.warning("Entity consolidate/agentic graph 실패(보고서는 계속 진행): %s", exc)
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
    sections, evidence_by_axis, mech_audit, gap_summary = post_write_gap_feedback(
        driver, work_id, card, sections, evidence_by_axis, mech_audit,
        axes=axes, evidence_k=evidence_k)
    overview, index_map, ov_violations = synthesize_overview(card, sections)
    mech_audit["overview"] = ov_violations
    return finalize_report(driver, work_id, sections, overview, index_map,
                           evidence_by_axis, mech_audit, out_dir=out_dir,
                           gap_feedback_summary=gap_summary,
                           pipeline_summary=pipeline_summary)
