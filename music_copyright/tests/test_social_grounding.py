"""시드-타임 사회신호 grounding 계약 — source_excerpt가 근거 텍스트에 실제 존재해야.

harvester가 웹 Fact에 거는 grounding 계약(source_excerpt verbatim)을 시드 단계
SocialTheme/Cause/PublicUse 추출에도 동일하게 적용한다. 2026-06-29 baseline에서
'청소년 정체성'처럼 원문에 없는 해석이 SocialTheme로 승격된 결함의 회귀 가드.
"""
from __future__ import annotations

import unittest

from music_copyright.graphflow.social import (
    _SocialItem,
    _SocialOut,
    _is_grounded,
    _keep_grounded,
)
from music_copyright.graphflow.agents.base import normalize_text

_CONTEXT = (
    "이 곡은 어려운 시기에 긍정과 기쁨을 제공하는 음악적 탈출구로 기능한다. "
    "BTS는 코로나19 팬데믹 동안 전 세계 팬들에게 위로를 전했다."
)


class SocialGroundingTest(unittest.TestCase):
    def _item(self, label: str, excerpt: str) -> _SocialItem:
        return _SocialItem(label=label, description=f"{label} 설명", source_excerpt=excerpt)

    def test_verbatim_excerpt_is_grounded(self) -> None:
        ctx = normalize_text(_CONTEXT)
        self.assertTrue(_is_grounded(
            self._item("긍정과 희망", "긍정과 기쁨을 제공하는 음악적 탈출구"), ctx))

    def test_hallucinated_interpretation_dropped(self) -> None:
        # 원문에 없는 해석 — baseline '청소년 정체성' 부류. 반드시 폐기.
        ctx = normalize_text(_CONTEXT)
        self.assertFalse(_is_grounded(
            self._item("청소년 정체성", "청소년의 정체성 형성을 다룬다"), ctx))

    def test_excerpt_matching_ignores_whitespace(self) -> None:
        # normalize_text가 공백을 제거하므로 띄어쓰기가 달라도 매칭된다.
        ctx = normalize_text(_CONTEXT)
        self.assertTrue(_is_grounded(
            self._item("위로", "코로나19  팬데믹  동안 전세계 팬들에게 위로를"), ctx))

    def test_too_short_excerpt_dropped(self) -> None:
        ctx = normalize_text(_CONTEXT)
        self.assertFalse(_is_grounded(self._item("긍정", "긍정"), ctx))

    def test_empty_label_dropped(self) -> None:
        ctx = normalize_text(_CONTEXT)
        self.assertFalse(_is_grounded(
            self._item("  ", "긍정과 기쁨을 제공하는 음악적 탈출구"), ctx))

    def test_keep_grounded_filters_all_lists(self) -> None:
        out = _SocialOut(
            themes=[
                self._item("긍정과 희망", "긍정과 기쁨을 제공하는 음악적 탈출구"),
                self._item("청소년 정체성", "청소년의 정체성 형성"),  # 환각 → drop
            ],
            causes=[self._item("코로나 위로", "코로나19 팬데믹 동안 전 세계 팬들에게 위로를 전했다")],
            public_uses=[self._item("교육 활용", "학교 교과서에 실렸다")],  # 원문에 없음 → drop
        )
        kept = _keep_grounded(out, _CONTEXT)
        self.assertEqual([t.label for t in kept.themes], ["긍정과 희망"])
        self.assertEqual([c.label for c in kept.causes], ["코로나 위로"])
        self.assertEqual(kept.public_uses, [])


if __name__ == "__main__":
    unittest.main()
