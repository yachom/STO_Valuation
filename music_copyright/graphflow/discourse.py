"""담론 큐레이션 (음악저작권) — STORM/Co-STORM. real_estate/discourse 미러.

관점 발견(Perspectivist) → 페르소나 라운드테이블 Q&A(Questioner↔Researcher, 공유
다이제스트로 전문가들이 서로 발견 위에 질문) → 아웃라인(Outliner). 고정 EXPAND를
담론 구조로 고도화한 경로 — curation.mode=discourse일 때만. static은 불변.

3축 척추(influence/popularity/sustainability)는 고정, 그 위에 발견 관점을 더한다(D18).
"""

from __future__ import annotations

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from neo4j import Driver
from pydantic import BaseModel, Field

from music_copyright.graphflow.agents.base import BaseAgent, music_system_prompt
from music_copyright.graphflow.graph.queries import work_card_text
from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.graphflow.prompts import get_prompt
from music_copyright.graphflow.traversal import scope_eids

logger = logging.getLogger("music_copyright.discourse")

_AXES = ("influence", "popularity", "sustainability")
# 라운드테이블에서 관점(thread)들을 동시에 전진시키는 스레드풀 동시성 상한.
# 관점 수(3축 + 발견 관점)는 보통 한 자릿수라 6이면 충분히 동시화된다.
_MAX_DISCOURSE_WORKERS = 6

# 3축 기본 페르소나/초점 (척추 — 항상 존재)
_BASE = {
    "influence": ("음악 평론·문화영향 분석가",
                  "비평·문화적 영향, 수상, 미디어 언급, 창작자(작곡·작사) 위상"),
    "popularity": ("차트·스트리밍 애널리스트",
                   "차트 성적, 스트리밍·플랫폼 도달, 청취자 규모, 팬덤 활동"),
    "sustainability": ("음악 IP·로열티 분석가",
                       "롱테일 스트리밍, 권리 카탈로그 안정성, 라이선스 다양성, 세대 전이"),
}


def discourse_max_rounds() -> int:
    from music_copyright.graphflow.config import get_config
    return int(get_config()["curation"]["turns_per_perspective"])


# ───────────────────────────────────────────────────────────
# 데이터 구조
# ───────────────────────────────────────────────────────────
@dataclass
class Perspective:
    parent_axis: str
    label: str
    focus: str
    persona: str
    qa: list[dict] = field(default_factory=list)
    done: bool = False


def _scope_anchor_names(driver: Driver, work_id: str, hops: int = 2) -> str:
    """스코프 내 실제 노드 이름 — Questioner 환각 방지용 앵커 목록."""
    eids = scope_eids(driver, work_id, hops)
    if not eids:
        return ""
    with driver.session() as s:
        rows = s.run(
            "UNWIND $eids AS eid MATCH (n) WHERE elementId(n)=eid "
            "RETURN labels(n)[0] AS label, coalesce(n.title, n.name, n.chart_name) AS nm",
            eids=list(eids),
        ).data()
    names = [f"{r['nm']}({r['label']})" for r in rows if r["nm"]]
    return ", ".join(dict.fromkeys(names))


# ───────────────────────────────────────────────────────────
# 에이전트
# ───────────────────────────────────────────────────────────
class PerspectiveDraft(BaseModel):
    parent_axis: str = Field(description="influence | popularity | sustainability 중 하나")
    label: str = Field(description="관점 이름 (예: '샘플·인용 영향')")
    focus: str = Field(description="이 관점이 파고들 초점 1문장")
    persona: str = Field(description="전문가 역할명")


class PerspectivesOut(BaseModel):
    perspectives: list[PerspectiveDraft] = Field(default_factory=list)


class Perspectivist(BaseAgent):
    name = "music_perspectivist"

    def discover(self, work_card: str, *, max_perspectives: int = 2) -> list[Perspective]:
        try:
            out: PerspectivesOut = self.complete(
                system=music_system_prompt(get_prompt("music_perspectivist.system")),
                user=f"[평가 대상]\n{work_card}\n\n3축 외에 추가로 볼 관점을 최대 "
                     f"{max_perspectives}개 제안하세요.",
                schema=PerspectivesOut)
        except Exception as exc:  # noqa: BLE001
            logger.warning("perspectivist 실패: %s", exc)
            return []
        res = []
        for p in out.perspectives[:max_perspectives]:
            axis = p.parent_axis if p.parent_axis in _AXES else "influence"
            res.append(Perspective(parent_axis=axis, label=p.label, focus=p.focus,
                                   persona=p.persona))
        return res


class QuestionTurn(BaseModel):
    question: str = Field(default="", description="이 관점에서 다음에 물을 구체적 질문 (없으면 빈 문자열)")
    done: bool = Field(default=False, description="더 물을 게 없으면 true")


class Questioner(BaseAgent):
    name = "music_questioner"

    def ask(self, persp: Perspective, *, work_card: str, scope_anchors: str,
            shared_digest: str) -> QuestionTurn:
        prior = "\n".join(f"  Q: {qa['q']}\n  A: {qa['a']}" for qa in persp.qa) or "(없음)"
        user = (f"[평가 대상]\n{work_card}\n\n[당신의 관점] {persp.label} — {persp.focus}\n"
                f"[스코프 내 실제 대상 — 이 이름들만 질문에 사용]\n{scope_anchors or '(없음)'}\n\n"
                f"[다른 전문가들의 최근 발견]\n{shared_digest or '(없음)'}\n\n"
                f"[지금까지 이 관점의 Q&A]\n{prior}\n\n"
                "다음에 물을 질문 1개. 이미 답한 것/근거 없는 추측 금지. 충분하면 done=true.")
        try:
            return self.complete(
                system=music_system_prompt(get_prompt("music_questioner.system")),
                user=user, schema=QuestionTurn)
        except Exception as exc:  # noqa: BLE001
            logger.warning("questioner 실패: %s", exc)
            return QuestionTurn(done=True)


class Researcher(BaseAgent):
    name = "music_researcher"

    def research(self, driver: Driver, work_id: str, question: str,
                 collected_by: str, *, budget: int = 3) -> str:
        """질문 → 웹검색(run_expand purpose) → 스코프 Fact 수집 → 압축 답변."""
        from music_copyright.graphflow.expand import run_expand
        try:
            run_expand(driver, work_id=work_id, collected_by=collected_by, rounds=1,
                       frontier_max=2, total_query_budget=budget, purpose=question)
        except Exception as exc:  # noqa: BLE001
            logger.debug("researcher expand skip: %s", exc)
        facts = _top_scope_facts(driver, work_id, limit=6)
        if not facts:
            return "관련 근거를 찾지 못했습니다."
        body = "\n".join(f"- {t}" for t in facts)
        try:
            # schema 없는 complete()는 AIMessage를 반환 — .content로 텍스트 추출.
            resp = self.complete(
                system=music_system_prompt(get_prompt("music_researcher.system")),
                user=f"[질문]\n{question}\n\n[수집 근거]\n{body}\n\n"
                     "근거에 있는 내용만으로 2~3문장 답하세요. 없으면 '근거 부족' 명시.")
            text = getattr(resp, "content", resp)
            return text if isinstance(text, str) else str(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("researcher 압축 실패: %s", exc)
            return facts[0][:200]


def _top_scope_facts(driver: Driver, work_id: str, *, limit: int = 6) -> list[str]:
    with driver.session() as s:
        rows = s.run(
            f"MATCH (w:{L.WORK} {{work_id: $wid}})-[*1..3]-(f:{L.FACT}) "
            "RETURN DISTINCT f.text AS t, f.confidence AS c ORDER BY c DESC LIMIT $k",
            wid=work_id, k=limit,
        ).data()
    return [r["t"] for r in rows if r["t"]]


class OutlineSection(BaseModel):
    axis: str = Field(description="influence | popularity | sustainability")
    points: list[str] = Field(default_factory=list, description="이 축 섹션의 논점(소제목)들")


class OutlineOut(BaseModel):
    sections: list[OutlineSection] = Field(default_factory=list)


class Outliner(BaseAgent):
    name = "music_outliner"

    def build(self, work_card: str, threads: list[dict], coverage_summary: str) -> dict[str, list[str]]:
        digest = []
        for th in threads:
            qas = "; ".join(f"{qa['q']}→{qa['a'][:60]}" for qa in th.get("qa", []))
            digest.append(f"[{th['axis']}/{th['label']}] {qas}")
        try:
            out: OutlineOut = self.complete(
                system=music_system_prompt(get_prompt("music_outliner.system")),
                user=f"[평가 대상]\n{work_card}\n\n[담론 Q&A]\n" + "\n".join(digest) +
                     f"\n\n[커버리지]\n{coverage_summary}\n\n"
                     "각 축(influence/popularity/sustainability)별 논점을 정리하세요.",
                schema=OutlineOut)
        except Exception as exc:  # noqa: BLE001
            logger.warning("outliner 실패: %s", exc)
            return {}
        result: dict[str, list[str]] = {}
        for sec in out.sections:
            if sec.axis in _AXES and sec.points:
                result.setdefault(sec.axis, []).extend(p for p in sec.points if p.strip())
        return result


# ───────────────────────────────────────────────────────────
# 스테이지
# ───────────────────────────────────────────────────────────
def perspectives_stage(driver: Driver, work_id: str, *,
                       perspectives_max: int | None = None) -> list[dict]:
    """3축 기본 관점 + 발견 관점 → thread 리스트."""
    from music_copyright.graphflow.config import get_config
    pmax = perspectives_max if perspectives_max is not None else \
        get_config()["curation"]["perspectives_max"]
    work_card = work_card_text(driver, work_id)
    threads = [{"axis": ax, "label": ax, "focus": _BASE[ax][1], "persona": _BASE[ax][0],
                "qa": [], "done": False} for ax in _AXES]
    for p in Perspectivist().discover(work_card, max_perspectives=pmax):
        threads.append({"axis": p.parent_axis, "label": p.label, "focus": p.focus,
                        "persona": p.persona, "qa": [], "done": False})
    logger.info("perspectives_stage: %d threads", len(threads))
    return threads


def _shared_digest(threads: list[dict], exclude_idx: int) -> str:
    lines = []
    for j, th in enumerate(threads):
        if j == exclude_idx or not th.get("qa"):
            continue
        last = th["qa"][-1]
        lines.append(f"- [{th['persona']}] {last['q'][:50]} → {last['a'][:80]}")
    return "\n".join(lines)


def _parallel_threads(advance_fn, items: list[tuple]) -> None:
    """관점 thread들을 bounded 스레드풀로 병렬 전진 (각 thread는 자기 dict만 in-place 갱신).

    advance_fn(idx, th)는 자기 th dict만 쓰므로 스레드 간 공유 가변상태가 없다. 트레이싱
    contextvar(_current_node/_axis/_tracer)는 워커로 자동 전파되지 않으므로 현재 컨텍스트를
    복사해 각 태스크를 그 안에서 실행한다(emit·토큰 roll-up·Langfuse span 스탬핑 보존).
    1건이면 풀 없이 인라인(오버헤드 회피). 각 thread의 researcher는 내부에서 harvester
    풀을 또 띄우지만(중첩), openai 호출은 게이트웨이가 전역 throttle(5/s)하고 Neo4j 풀은
    충분(기본 100)해 안전하다.
    """
    if not items:
        return
    if len(items) == 1:
        advance_fn(*items[0])
        return
    workers = min(_MAX_DISCOURSE_WORKERS, len(items))

    def _run_in_ctx(item: tuple):
        ctx = contextvars.copy_context()
        return ctx.run(lambda: advance_fn(*item))

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="discourse") as pool:
        list(pool.map(_run_in_ctx, items))


def discourse_round_stage(driver: Driver, work_id: str, threads: list[dict],
                          collected_by: str, *, research_budget: int | None = None) -> list[dict]:
    """라운드테이블 1턴 — 각 관점이 질문→연구로 qa 1건씩 추가 (in-place, 병렬).

    관점들을 스레드풀로 동시에 전진시킨다. 각 관점이 보는 shared_digest는 dispatch 전에
    고정(round-start 스냅샷)해, 직렬판의 "앞 관점의 이번 라운드 답을 뒤 관점이 본다"는
    순서 의존을 없애고 모든 관점이 동일 상태에서 출발하는 동시 라운드테이블로 만든다.
    """
    from music_copyright.graphflow.config import get_config
    budget = research_budget if research_budget is not None else \
        get_config()["curation"]["research_budget"]
    work_card = work_card_text(driver, work_id)
    anchors = _scope_anchor_names(driver, work_id)
    # round-start 스냅샷: 병렬 실행 중 다른 thread의 qa 갱신과 digest read가 경합하지 않게.
    digests = [_shared_digest(threads, i) for i in range(len(threads))]
    items = [(i, th) for i, th in enumerate(threads) if not th.get("done")]

    def _advance(idx: int, th: dict) -> None:
        # Questioner/Researcher는 무상태라 태스크별 생성(스레드 격리).
        questioner, researcher = Questioner(), Researcher()
        persp = Perspective(parent_axis=th["axis"], label=th["label"],
                            focus=th["focus"], persona=th["persona"], qa=th["qa"])
        turn = questioner.ask(persp, work_card=work_card, scope_anchors=anchors,
                              shared_digest=digests[idx])
        if turn.done or not turn.question.strip():
            th["done"] = True
            return
        answer = researcher.research(driver, work_id, turn.question, collected_by,
                                     budget=budget)
        th["qa"].append({"q": turn.question, "a": answer})

    _parallel_threads(_advance, items)
    return threads


def outline_stage(driver: Driver, work_id: str, threads: list[dict]) -> dict:
    """담론 thread + 커버리지 → 축별 아웃라인."""
    from music_copyright.graphflow.coverage import axis_coverage, coverage_summary_text
    work_card = work_card_text(driver, work_id)
    cov_text = coverage_summary_text(axis_coverage(driver, work_id))
    outline = Outliner().build(work_card, threads, cov_text)
    logger.info("outline_stage: %s", {k: len(v) for k, v in outline.items()})
    return outline
