"""BaseSynthesizerAgent — 섹션→Overview 종합 역할베이스 (+ format_sections 자유함수).

두 자산이 바이트 동일하게 갖던 _format_sections를 자유함수 format_sections로 통합하고,
synthesize() 호출 골격을 베이스로 올린다. 자산이 다른 부분은 hook으로 받는다:
    Schema       : 자산별 OverviewDraft (음악은 자산/사회 이중 트랙 슈퍼셋 — 통합 안 함)
    role_key/rules_key, card_label, sections_label
    _get_prompt / _frame / _build_user : 오버라이드 hook (음악은 [채점할 축] 목록 추가)
"""

from __future__ import annotations

import logging
from typing import Any

from infra.agent.base import BaseAgent

logger = logging.getLogger("infra.agent.roles.synthesizer")


def format_sections(sections) -> tuple[str, list[tuple[str, int]]]:
    """섹션들을 [C#] 글로벌 인덱스 텍스트로 직렬화. 반환: (텍스트, C#→(axis, claim_idx)).

    sections = {axis: SectionDraft-유사(title·blocks[].markdown·claims[].kind/text/as_of)}.
    duck-typed — 자산별 SectionDraft에 의존하지 않는다.
    """
    lines: list[str] = []
    index_map: list[tuple[str, int]] = []
    c = 0
    for axis, sec in sections.items():
        lines.append(f"\n## [{axis}] {sec.title}")
        for b in sec.blocks:
            lines.append(b.markdown)
        lines.append("주장 목록:")
        for i, cl in enumerate(sec.claims):
            c += 1
            index_map.append((axis, i))
            as_of = f" (기준 {cl.as_of})" if cl.as_of else ""
            lines.append(f"  [C{c}] ({cl.kind}) {cl.text}{as_of}")
    return "\n".join(lines), index_map


class BaseSynthesizerAgent(BaseAgent):
    """섹션→Overview 종합 역할베이스. 서브클래스가 스키마·슬롯·프레임·라벨을 공급한다."""

    Schema: type = None
    role_key: str = ""
    rules_key: str = ""
    card_label: str = "[평가 대상 자산]"
    sections_label: str = "[3축 섹션과 주장 목록]"

    # ── 자산 도메인 hook ──
    def _get_prompt(self, key: str, **fmt) -> str:
        raise NotImplementedError

    def _frame(self, role: str) -> str:
        """시스템 프레임 래퍼. 기본=프레임 없음 — 서브클래스가 오버라이드."""
        return role

    def _build_user(self, card: str, sections_text: str, sections) -> str:
        """기본 user 조립(부동산형). 음악은 [채점할 축] 목록을 덧붙이려 오버라이드한다."""
        return f"{self.card_label}\n{card}\n\n{self.sections_label}\n{sections_text}"

    # ── 골격 ──
    def synthesize(self, card: str, sections) -> tuple[Any, list[tuple[str, int]]]:
        """Overview 생성. 반환: (draft, C# 글로벌 인덱스 → (axis, claim_idx) 매핑)."""
        sections_text, index_map = format_sections(sections)
        system = self._frame(self._get_prompt(self.role_key)) + self._get_prompt(self.rules_key)
        user = self._build_user(card, sections_text, sections)
        out = self.complete(system=system, user=user, schema=self.Schema)
        logger.info("%s: blocks=%d claims=%d", self.name, len(out.blocks), len(out.claims))
        return out, index_map
