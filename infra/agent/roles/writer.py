"""BaseWriterAgent — 축별 섹션 집필 역할베이스 (자산 무관 호출 골격).

부동산·음악 Writer가 공유하던 write() 골격을 여기로 올린다:

    title/focus 해소 → system(frame + role + rules) → user(card·증거·지시·outline?·violations?)
    → complete(schema) → 로그

자산이 다른 부분은 서브클래스가 클래스변수/오버라이드로 공급한다:
    Schema        : 자산별 SectionDraft (Field description이 LLM 입력이라 통합 안 함)
    role_key/rules_key : get_prompt 키 (예 "writer.role" / "music_writer.role")
    AXIS_TITLES/AXIS_FOCUS : 축→제목/초점 표
    card_label    : 사용자 프롬프트 카드 라벨 (예 "[평가 대상 자산]" / "[평가 대상 저작물]")
    outline_intro : 아웃라인 블록 안내문
    strict_axis   : True=AXIS_TITLES[axis](미지정 축은 KeyError) / False=.get(axis, 폴백)
    _get_prompt / _format_evidence / _frame : 자산 함수로 오버라이드하는 hook
"""

from __future__ import annotations

import logging
from typing import Any

from infra.agent.base import BaseAgent

logger = logging.getLogger("infra.agent.roles.writer")


class BaseWriterAgent(BaseAgent):
    """섹션 집필 역할베이스. 서브클래스가 아래 클래스변수·hook을 채운다."""

    # ── 서브클래스 필수 클래스변수 ──
    Schema: type = None              # 자산별 SectionDraft
    role_key: str = ""               # 시스템 역할 프롬프트 키 (title/focus 치환)
    rules_key: str = ""              # 시스템 규칙 프롬프트 키
    AXIS_TITLES: dict[str, str] = {}
    AXIS_FOCUS: dict[str, str] = {}

    # ── 오버라이드 가능 (기본은 부동산 수집형 동작) ──
    card_label: str = "[평가 대상 자산]"
    outline_intro: str = (
        "[아웃라인 — 이 논점들을 순서대로 다루며 구성하세요. 단 각 논점은 위 "
        "증거가 뒷받침하는 범위에서만 — 증거 없는 논점은 '근거 부족'으로 명시]")
    strict_axis: bool = True

    # ── 자산 도메인 hook (서브클래스가 자산 함수로 오버라이드) ──
    def _get_prompt(self, key: str, **fmt) -> str:
        """자산 graphflow.prompts.get_prompt."""
        raise NotImplementedError

    def _format_evidence(self, evidence) -> str:
        """자산 graphflow.composer.format_evidence."""
        raise NotImplementedError

    def _frame(self, role: str) -> str:
        """시스템 프레임 래퍼. 기본은 프레임 없음(원문 그대로) — 음악·부동산 Writer는 오버라이드."""
        return role

    # ── 골격 ──
    def _resolve_title_focus(self, axis, title, focus):
        if self.strict_axis:
            return title or self.AXIS_TITLES[axis], focus or self.AXIS_FOCUS[axis]
        return (title or self.AXIS_TITLES.get(axis, f"{axis} 분석"),
                focus or self.AXIS_FOCUS.get(axis, ""))

    def write(
        self,
        axis: str,
        card: str,
        evidence: list,
        *,
        violations: list[str] | None = None,
        outline: list[str] | None = None,
        title: str | None = None,
        focus: str | None = None,
    ) -> Any:
        """섹션 1개 집필. violations면 재작성 모드. outline 있으면 그 순서로 구성.

        title/focus 미지정이면 축 표에서 룩업, 지정되면 그대로(동적 축 — 라벨·의도 전달).
        """
        title, focus = self._resolve_title_focus(axis, title, focus)
        system = self._frame(
            self._get_prompt(self.role_key, title=title, focus=focus)
        ) + self._get_prompt(self.rules_key)
        parts = [
            f"{self.card_label}\n{card}",
            f"[증거 — 그래프 walk 수집, 경로 포함]\n{self._format_evidence(evidence)}",
            "위 증거만으로 섹션을 작성하세요. 증거가 부족한 논점은 부족하다고 명시하세요.",
        ]
        if outline:
            parts.append(self.outline_intro + "\n" + "\n".join(f"- {p}" for p in outline))
        if violations:
            parts.append("[재작성 — 직전 출력의 위반 사항을 반드시 수정]\n"
                         + "\n".join(f"- {v}" for v in violations))
        out = self.complete(system=system, user="\n\n".join(parts), schema=self.Schema)
        logger.info("%s(%s): blocks=%d claims=%d%s", self.name, axis,
                    len(out.blocks), len(out.claims), " (재작성)" if violations else "")
        return out
