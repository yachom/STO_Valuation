"""사회적 가치 신호 추출 (음악저작권) — 시드-타임 LLM 추출기.

음악에는 '사회적 가치'를 직접 주는 정형 공개 API가 없다(차트·수상·스트리밍은 전부
상업·위신 신호). 작품의 사회적 의제·메시지·공익 연계는 가사/에디토리얼 텍스트에
묻혀 있으므로, 시드 단계에서 *근거가 있는 텍스트*(Genius 'About' 설명 + 작품 카드)를
보고 보수적으로 추출해 구조 노드(SocialTheme/Cause/PublicUse)로 적재한다.

설계 원칙:
- 제공된 텍스트에 *명시·강하게 시사*된 것만. 일반론·추측·곡 평판으로 지어내기 금지.
- 근거 텍스트가 비면 호출부가 통째 스킵(빈 입력으로 환각 유도하지 않음).
- best-effort: 실패/키부재는 빈 결과로 흡수(시드를 막지 않음). gpt_mini 티어.

웹검색(EXPAND)이 추가로 사회 Fact를 모으는 경로는 별도다(navigator/harvester 프롬프트가
사회적 가치 질의로 조타됨). 여기서는 시드-타임 정형 노드만 만든다.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from music_copyright.graphflow.agents.base import (
    BaseAgent,
    music_system_prompt,
    normalize_text,
)
from music_copyright.graphflow.prompts import get_prompt

logger = logging.getLogger("music_copyright.social")

# source_excerpt가 근거 텍스트에 실제로 존재한다고 보려면 최소 길이 — 우연한 짧은
# 부분일치(조사·접속사)를 grounding으로 오인하지 않게. harvester._fact_grounded_in_doc와 동일.
_MIN_EXCERPT = 8


class _SocialItem(BaseModel):
    label: str = Field(description="짧은 이름 (예: '정신건강 인식', 'BLM 연대', '졸업식 단골곡')")
    description: str = Field(default="", description="근거 텍스트에 비춘 1문장 설명")
    source_excerpt: str = Field(
        default="",
        description="[근거 텍스트]에 그대로(verbatim) 존재하는 구절. 축약·의역·번역 금지. "
                    "이 신호의 직접 근거이며, 텍스트에 없으면 그 항목은 폐기된다.")


class _SocialOut(BaseModel):
    themes: list[_SocialItem] = Field(
        default_factory=list,
        description="작품이 다루는 사회적 의제·메시지 (정의·인권·정신건강·환경·정체성·위로 등)")
    causes: list[_SocialItem] = Field(
        default_factory=list,
        description="연계된 사회운동·시위·공익/자선 캠페인 (앤섬화·기부·연대 등)")
    public_uses: list[_SocialItem] = Field(
        default_factory=list,
        description="공익·교육·기념·공공행사·문화외교 등 공적 활용 사례")


def _is_grounded(item: _SocialItem, context_norm: str) -> bool:
    """source_excerpt가 근거 텍스트에 실제로(공백·대소문자 무시) 존재하는지 검사.

    harvester가 웹 Fact에 거는 grounding 계약을 시드-타임 사회신호에도 동일하게 적용한다.
    엔티티명·곡명 일치만으로는 함의를 보장하지 못하므로, 추출기가 제시한 원문 구절이
    실제 근거 텍스트에 존재해야 한다.
    """
    if not item.label.strip():
        return False
    excerpt = normalize_text(item.source_excerpt or "")
    return len(excerpt) >= _MIN_EXCERPT and excerpt in context_norm


def _keep_grounded(out: "_SocialOut", context_text: str) -> "_SocialOut":
    context_norm = normalize_text(context_text)
    kept = _SocialOut(
        themes=[it for it in out.themes if _is_grounded(it, context_norm)],
        causes=[it for it in out.causes if _is_grounded(it, context_norm)],
        public_uses=[it for it in out.public_uses if _is_grounded(it, context_norm)],
    )
    dropped = ((len(out.themes) - len(kept.themes))
               + (len(out.causes) - len(kept.causes))
               + (len(out.public_uses) - len(kept.public_uses)))
    if dropped:
        logger.info("social_extractor: source_excerpt 불충족 %d개 항목 폐기", dropped)
    return kept


class SocialValueExtractor(BaseAgent):
    """근거 텍스트에서 작품의 사회적 가치 신호를 보수적으로 추출."""

    name = "music_social_extractor"

    def extract(self, *, title: str, artist: str, context_text: str,
                max_each: int = 4) -> _SocialOut:
        if not (context_text or "").strip():
            return _SocialOut()
        user = (
            f"[작품] {title} — {artist}\n\n"
            f"[근거 텍스트 — 이 안에 명시·시사된 것만 추출]\n{context_text.strip()}\n\n"
            f"위 근거에서 이 작품의 *사회적 가치* 신호를 추출하세요. 각 분류 최대 {max_each}개.\n"
            "- themes: 작품이 다루는 사회적 의제·메시지\n"
            "- causes: 연계된 사회운동·시위·공익/자선 캠페인\n"
            "- public_uses: 공익·교육·기념·공공·문화외교 활용\n"
            "근거에 없거나 단순 상업 성과(차트·판매·스트리밍)면 넣지 마세요. 없으면 빈 배열.\n"
            "각 항목의 source_excerpt에는 [근거 텍스트]의 구절을 **그대로 복사**하세요"
            "(의역·번역·요약 금지). verbatim 구절이 없는 항목은 폐기됩니다.")
        try:
            out = self.complete(
                system=music_system_prompt(get_prompt("music_social_extractor.system")),
                user=user, schema=_SocialOut)
        except Exception as exc:  # noqa: BLE001 — best-effort, 시드를 막지 않음
            logger.warning("social_extractor 실패: %s", exc)
            return _SocialOut()
        # grounding: source_excerpt가 근거 텍스트에 실제 존재하는 항목만 통과(환각 차단).
        return _keep_grounded(out, context_text)


def extract_social_signals(*, title: str, artist: str, context_text: str,
                           max_each: int = 4) -> _SocialOut:
    """편의 함수 — SocialValueExtractor.extract 단발 호출."""
    return SocialValueExtractor().extract(
        title=title, artist=artist, context_text=context_text, max_each=max_each)
