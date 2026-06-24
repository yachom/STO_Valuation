"""Moderator (음악저작권) — Co-STORM 모더레이터. real_estate/moderator 미러.

커버리지 측정 + 수집 Fact 표본 → 비어 있는 축의 unknown-unknowns 질문 → GAP을
미니 EXPAND로 재진입 채움. Auditor(모순 탐지)와 역할이 다르다 — 여기는 커버리지 공백.
"""

from __future__ import annotations

import logging

from neo4j import Driver
from pydantic import BaseModel, Field

from music_copyright.graphflow.agents.base import BaseAgent, music_system_prompt
from music_copyright.graphflow.graph.queries import work_card_text
from music_copyright.graphflow.prompts import get_prompt

logger = logging.getLogger("music_copyright.moderator")


class GapQuestion(BaseModel):
    axis: str = Field(description="influence | popularity | sustainability")
    question: str = Field(description="웹검색 가능한 구체적 질문")
    why: str = Field(default="", description="이게 공백인 이유 1문장")


class ModeratorVerdict(BaseModel):
    done: bool = Field(default=False, description="커버리지가 충분하면 true")
    rationale: str = Field(default="")
    questions: list[GapQuestion] = Field(default_factory=list)


class Moderator(BaseAgent):
    name = "music_moderator"

    def assess(self, work_card: str, coverage_text: str,
               fact_samples: dict[str, list[str]] | None = None, *,
               max_questions: int = 3) -> ModeratorVerdict:
        samples = ""
        if fact_samples:
            samples = "\n".join(f"[{ax}] " + " / ".join(t[:50] for t in ts)
                                for ax, ts in fact_samples.items() if ts)
        try:
            v: ModeratorVerdict = self.complete(
                system=music_system_prompt(get_prompt("music_moderator.system")),
                user=(f"[평가 대상]\n{work_card}\n\n[커버리지]\n{coverage_text}\n\n"
                      f"[수집된 사실 표본]\n{samples or '(없음)'}\n\n"
                      f"비어 있는 축을 메울 질문을 최대 {max_questions}개. 충분하면 done=true."),
                schema=ModeratorVerdict)
            v.questions = v.questions[:max_questions]
            return v
        except Exception as exc:  # noqa: BLE001
            logger.warning("moderator 실패: %s", exc)
            return ModeratorVerdict(done=True)


def moderator_round_stage(driver: Driver, work_id: str, round_idx: int,
                          max_rounds: int, collected_by: str) -> dict:
    """커버리지 → verdict → GAP 미니 EXPAND. 반환 {done, questions}."""
    from music_copyright.graphflow.config import get_config
    from music_copyright.graphflow.coverage import (
        axis_coverage, axis_fact_samples, coverage_summary_text)
    from music_copyright.graphflow.expand import run_expand

    cur = get_config()["curation"]
    cov = axis_coverage(driver, work_id)
    verdict = Moderator().assess(
        work_card_text(driver, work_id), coverage_summary_text(cov),
        axis_fact_samples(driver, work_id), max_questions=cur["moderator_max_questions"])

    if not verdict.done and verdict.questions:
        per_q = max(1, cur["moderator_budget_per_round"] // len(verdict.questions))
        for gq in verdict.questions:
            try:
                run_expand(driver, work_id=work_id, collected_by=collected_by, rounds=1,
                           frontier_max=2, total_query_budget=per_q, purpose=gq.question)
            except Exception as exc:  # noqa: BLE001
                logger.debug("moderator gap expand skip: %s", exc)

    done = bool(verdict.done or not verdict.questions or round_idx >= max_rounds)
    logger.info("moderator round %d/%d: done=%s questions=%d",
                round_idx, max_rounds, done, len(verdict.questions))
    return {"done": done, "questions": len(verdict.questions)}


def run_moderated_expand(driver: Driver, *, work_id: str, collected_by: str,
                         max_rounds: int | None = None) -> dict:
    """모더레이터 GAP 루프 (static EXPAND 후 보강). curation.mode=moderated 경로용."""
    from music_copyright.graphflow.config import get_config
    cur = get_config()["curation"]
    max_rounds = cur["moderator_max_rounds"] if max_rounds is None else max_rounds
    history = []
    for r in range(1, max_rounds + 1):
        res = moderator_round_stage(driver, work_id, r, max_rounds, collected_by)
        history.append({"round": r, **res})
        if res["done"]:
            break
    return {"rounds_run": len(history), "history": history}
