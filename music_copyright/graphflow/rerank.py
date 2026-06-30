"""근거 재랭킹 (음악저작권) — 키워드 룰 더미 대신 LLM이 축별 근거를 선별.

composer의 그래프 walk + 벡터검색이 넓게 모은 후보(EvidencePath→Evidence)를 받아,
평가 대상 작품과 축 의도를 보고 '이 축을 평가하는 데 가장 적합한 근거'를 LLM이 고른다.
판단 기준(축 관련성·대상 일치·사실성·출처 품질·중복 회피)은 코드 점수표가 아니라
프롬프트로 둔다 — 키워드 그룹·출처 감점·맥락 감점 같은 규칙 누적을 대체한다.

LLM 실패/빈 응답이면 walk·벡터 원점수 순으로 폴백한다(보고서 산출은 멈추지 않는다).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from music_copyright.graphflow.agents.base import BaseAgent, music_system_prompt
from music_copyright.graphflow.prompts import get_prompt

if TYPE_CHECKING:
    from music_copyright.graphflow.composer import Evidence

logger = logging.getLogger("music_copyright.rerank")

_MAX_CANDIDATES = 40          # LLM에 보낼 후보 상한(프롬프트 크기 제어)
_TEXT_TRIM = 300


class RerankOut(BaseModel):
    selected: list[int] = Field(
        default_factory=list,
        description="가장 적합한 근거부터 순서대로 고른 후보 번호(1-based). 관련 근거가 "
                    "부족하면 요청 개수보다 적게 골라도 된다.")


class MusicReranker(BaseAgent):
    name = "music_reranker"


def _format_candidates(candidates: list) -> str:
    lines = []
    for i, ev in enumerate(candidates, 1):
        src = (ev.origin or {}).get("source") or ev.source_kind
        url = (ev.origin or {}).get("url") or ""
        text = (ev.text or "").replace("\n", " ").strip()[:_TEXT_TRIM]
        meta = f"종류={ev.source_kind}" + (f", 출처={src}" if src else "") + (f", url={url}" if url else "")
        lines.append(f"[{i}] ({meta})\n{text}")
    return "\n\n".join(lines)


def _reindex(selected: list) -> list:
    for j, ev in enumerate(selected, 1):
        ev.index = j
    return selected


def rerank_evidence(
    work_card: str,
    axis_key: str,
    axis_intent: str,
    candidates: list["Evidence"],
    *,
    k: int,
) -> list["Evidence"]:
    """후보 Evidence를 LLM이 축별로 선별·정렬해 상위 k개 반환(재인덱싱 E1..Ek).

    실패/빈 응답이면 원점수(score) 내림차순 상위 k로 폴백한다. 결정성은 포기하되
    (LLM 판단), 보고서 산출 안정성은 폴백으로 보장한다.
    """
    if not candidates:
        return []
    pool = list(candidates)[:_MAX_CANDIDATES]

    def _fallback() -> list:
        ordered = sorted(candidates, key=lambda e: (-e.score, e.ref))[:k]
        return _reindex(ordered)

    try:
        user = (
            f"[평가 대상 작품]\n{work_card}\n\n"
            f"[평가 축] {axis_key}\n[이 축이 평가하려는 것]\n{axis_intent}\n\n"
            f"[후보 근거 {len(pool)}개]\n{_format_candidates(pool)}\n\n"
            f"위 후보 중 이 축을 평가하는 데 가장 적합한 근거를 가장 좋은 것부터 순서대로 "
            f"최대 {k}개, 번호로 고르세요."
        )
        out: RerankOut = MusicReranker().complete(
            system=music_system_prompt(get_prompt("music_reranker.system")),
            user=user, schema=RerankOut)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rerank LLM 실패(점수순 폴백): %s", exc)
        return _fallback()

    seen: set[int] = set()
    ordered: list = []
    for i in out.selected:
        if isinstance(i, int) and 1 <= i <= len(pool) and i not in seen:
            seen.add(i)
            ordered.append(pool[i - 1])
    if not ordered:
        logger.info("rerank 빈 선택 → 점수순 폴백")
        return _fallback()
    logger.info("rerank(%s): %d/%d 후보 선별", axis_key, min(len(ordered), k), len(pool))
    return _reindex(ordered[:k])
