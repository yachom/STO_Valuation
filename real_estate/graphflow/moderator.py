"""Moderator — 커버리지 기반 unknown-unknowns 사냥 + GAP→미니EXPAND 재진입 (D19).

Co-STORM 모더레이터: 비전문가지만 '무엇이 안 다뤄졌나'를 좋은 질문으로 캔다.
auditor(모순·과대해석)와 **역할 분리** — moderator는 커버리지 공백만 본다.

흐름 (run_moderated_expand):
    coverage(그래프 측정) → Moderator(빈 곳 질문 생성 or 종료) →
    각 질문을 run_expand(purpose=질문, 축소예산)로 미니 EXPAND 재진입 → 반복.
종료: moderator의 done 판정 / max_rounds 소진 / 질문 없음.
이는 D7(QA 자가확장)의 일반화 — 오래 미뤄둔 GAP→미니EXPAND를 원리적으로 배선.

재현성(D20): 비결정적이라 config의 curation.mode == 'moderated'에서만 발동한다.
기본 static이면 기존 결정 경로(StaticPolicy)가 그대로 — workflow.expand_node가 게이트.
검색 기반으로만 공백을 채운다(D22) — 질문은 웹/뉴스 검색으로 답할 수 있는 것만.
"""

from __future__ import annotations

import logging

from neo4j import Driver
from pydantic import BaseModel, Field, field_validator

from real_estate.graphflow.agents.base import BaseAgent, coerce_json
from real_estate.graphflow.config import get_config
from real_estate.graphflow.coverage import (
    AxisCoverage,
    axis_coverage,
    axis_fact_samples,
    coverage_summary_text,
    emit_coverage_scores,
)
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.prompts import get_prompt
from infra.agent.tracing import add_score, emit, node_scope

logger = logging.getLogger("graphflow.moderator")


# ───────────────────────────────────────────────────────────
# 스키마
# ───────────────────────────────────────────────────────────
class GapQuestion(BaseModel):
    axis: str = Field(description="이 질문이 메우는 축 — tenant/esg/commercial 중 하나")
    question: str = Field(
        description="공백을 메울 검색 질문. 웹/뉴스 검색으로 답할 수 있어야 하고 "
                    "자산의 고유명사(상권명·건물명·인증·업종 등)를 담아 날카롭게")
    why: str = Field(description="어떤 커버리지 공백을 왜 메우는지 한 문장")


class ModeratorVerdict(BaseModel):
    done: bool = Field(
        description="세 축이 충분히 커버됐거나 추가 검색이 도움 안 되면 true")
    rationale: str = Field(description="판단 근거 한두 문장")
    questions: list[GapQuestion] = Field(
        default_factory=list,
        description="공백을 메울 질문들. done=true면 빈 리스트")

    @field_validator("questions", mode="before")
    @classmethod
    def _coerce(cls, v):
        return coerce_json(v)


class Moderator(BaseAgent):
    name = "moderator"

    def assess(
        self, asset_card: str, cov: dict[str, AxisCoverage], *,
        fact_samples: dict[str, list[str]] | None = None, max_questions: int = 3,
    ) -> ModeratorVerdict:
        """커버리지 신호(+수집 사실 표본) → 공백 메우는 질문 또는 종료 판정 (LLM 1회)."""
        summary = coverage_summary_text(cov)
        samples_block = ""
        if fact_samples:
            lines = [f"[{ax}] " + " / ".join(t[:80] for t in facts)
                     for ax, facts in fact_samples.items() if facts]
            if lines:
                samples_block = ("\n[지금까지 수집된 사실 표본 — 이것들이 *시사하지만* "
                                 "담론이 아직 안 파고든 방향, 또는 표본 간 연결/모순을 "
                                 "질문으로]\n" + "\n".join(lines) + "\n")
        user = (
            f"[평가 대상 자산]\n{asset_card}\n\n"
            f"[관점별 그래프 커버리지 — 어디가 비었나]\n{summary}\n"
            + samples_block + "\n"
            f"빈 곳(Fact 전무/희박·미검색·노후)을 메우거나 수집 표본이 시사하는 "
            f"unknown-unknown을 캘 검색 질문을 최대 {max_questions}개 고르세요. 세 축이 "
            f"충분하거나 검색으로 메우기 어려우면 done=true로 종료하세요.")
        verdict: ModeratorVerdict = self.complete(
            system=get_prompt("moderator.system"), user=user, schema=ModeratorVerdict)
        verdict.questions = verdict.questions[:max_questions]   # 상한 강제
        return verdict


# ───────────────────────────────────────────────────────────
# GAP → 미니 EXPAND 재진입 루프
# ───────────────────────────────────────────────────────────
def _asset_card(driver: Driver, asset_id: str) -> str:
    with driver.session() as s:
        r = s.run(f"MATCH (a:{L.ASSET} {{asset_id: $aid}}) RETURN a.card_text AS c",
                  aid=asset_id).single()
    return (r["c"] if r else None) or asset_id


def run_moderated_expand(
    driver: Driver,
    *,
    asset_id: str,
    collected_by: str,
    max_rounds: int | None = None,
    budget_per_round: int | None = None,
    max_questions: int | None = None,
) -> dict:
    """커버리지 GAP → Moderator 질문 → 미니 EXPAND 재진입 루프.

    기존 EXPAND 위에 얹는 추가 라운드(additive, 기존 결정 경로 불변). 캡은 인자가
    None이면 config.curation에서 읽는다. 반환: 라운드 통계 + 최종 커버리지 요약.

    이중 종료(발산 방지): moderator의 done 판정 + max_rounds 하드캡.
    각 질문은 qa.py 자가확장과 동일하게 run_expand(purpose=, 축소예산)로 재진입.
    """
    cur = get_config().get("curation", {})
    max_rounds = int(max_rounds if max_rounds is not None
                     else cur.get("moderator_max_rounds", 2))
    budget_per_round = int(budget_per_round if budget_per_round is not None
                           else cur.get("moderator_budget_per_round", 6))
    max_questions = int(max_questions if max_questions is not None
                        else cur.get("moderator_max_questions", 3))

    asset_card = _asset_card(driver, asset_id)
    moderator = Moderator()
    history: list[dict] = []

    from real_estate.graphflow.expand import run_expand   # 지연 import (순환 회피)

    for rnd in range(1, max_rounds + 1):
        with node_scope(f"moderator_round_{rnd}"):
            cov = axis_coverage(driver, asset_id)
            emit_coverage_scores(cov)
            verdict = moderator.assess(asset_card, cov, max_questions=max_questions,
                                       fact_samples=axis_fact_samples(driver, asset_id))
            emit("moderator_verdict", round=rnd, done=verdict.done,
                 n_questions=len(verdict.questions), rationale=verdict.rationale[:160])

            if verdict.done or not verdict.questions:
                history.append({"round": rnd, "done": True, "questions": 0,
                                "reason": verdict.rationale[:120]})
                break

            per_q = max(1, budget_per_round // len(verdict.questions))
            for gq in verdict.questions:
                emit("moderator_gap_expand", round=rnd, axis=gq.axis,
                     question=gq.question[:100])
                run_expand(driver, asset_id=asset_id, collected_by=collected_by,
                           purpose=gq.question, rounds=1, frontier_max=2,
                           total_query_budget=per_q)
            history.append({"round": rnd, "done": False,
                            "questions": len(verdict.questions),
                            "axes": [gq.axis for gq in verdict.questions]})

    final = axis_coverage(driver, asset_id)
    emit_coverage_scores(final)
    add_score("moderator_rounds_run", len(history))
    logger.info("moderated EXPAND 완료 — rounds=%d", len(history))
    return {
        "rounds_run": len(history),
        "history": history,
        "final_coverage": {ax: {"facts": ac.n_facts, "press": ac.n_press,
                                "uncited": len(ac.uncited), "stale": ac.n_stale}
                           for ax, ac in final.items()},
    }
