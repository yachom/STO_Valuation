"""골든 프롬프트 캡처 — 공통 에이전트를 infra 역할베이스로 끌어올릴 때 동작 보존을 증명한다.

방법: BaseAgent.complete를 가로채 (system, user, schema)를 캡처하고 sentinel을 던진다.
각 에이전트의 메서드를 고정 stub 입력으로 호출하면, LLM 호출 직전에 조립된 정확한
프롬프트가 잡힌다. 기대값은 테스트 안에서 자산 프롬프트(get_prompt)·프레임 함수로
독립 재구성한다 — 리팩토링이 system/user/schema를 1바이트라도 바꾸면 즉시 깨진다.

리팩토링 전(현재 코드)에도, 후(역할베이스)에도 동일하게 통과해야 한다.
"""

from __future__ import annotations

import pytest

from infra.agent.base import BaseAgent


class _Captured(Exception):
    """complete() 호출 직전 조립된 프롬프트를 실어 흐름을 멈춘다."""

    def __init__(self, system, user, schema):
        super().__init__("captured")
        self.system = system
        self.user = user
        self.schema = schema


@pytest.fixture
def capture(monkeypatch):
    """BaseAgent.complete를 가로채 (system,user,schema)만 캡처하고 _Captured를 던진다."""

    def fake_complete(self, *, system, user, schema=None, **_kw):
        raise _Captured(system, user, schema)

    monkeypatch.setattr(BaseAgent, "complete", fake_complete)


# ───────────────────────────────────────────────────────────
# Writer — 축별 섹션 집필
# ───────────────────────────────────────────────────────────
def test_music_writer_prompt(capture):
    from music_copyright.graphflow.agents.base import music_system_prompt
    from music_copyright.graphflow.composer import format_evidence
    from music_copyright.graphflow.prompts import get_prompt
    from music_copyright.graphflow.writer import (
        AXIS_FOCUS,
        AXIS_TITLES,
        MusicWriter,
        SectionDraft,
    )

    with pytest.raises(_Captured) as ei:
        MusicWriter().write("influence", "CARD-X", [], outline=["p1", "p2"],
                            violations=["v1"])
    cap = ei.value

    assert cap.schema is SectionDraft
    exp_system = music_system_prompt(
        get_prompt("music_writer.role", title=AXIS_TITLES["influence"],
                   focus=AXIS_FOCUS["influence"])) + get_prompt("music_writer.rules")
    assert cap.system == exp_system
    exp_user = "\n\n".join([
        "[평가 대상 저작물]\nCARD-X",
        f"[증거 — 그래프 walk 수집, 경로 포함]\n{format_evidence([])}",
        "위 증거만으로 섹션을 작성하세요. 증거가 부족한 논점은 부족하다고 명시하세요.",
        "[아웃라인 — 이 논점들을 순서대로, 단 증거가 뒷받침하는 범위에서만]\n- p1\n- p2",
        "[재작성 — 직전 출력의 위반 사항을 반드시 수정]\n- v1",
    ])
    assert cap.user == exp_user


def test_real_estate_writer_prompt(capture):
    from real_estate.graphflow.agents.base import korean_system_prompt
    from real_estate.graphflow.composer import format_evidence
    from real_estate.graphflow.prompts import get_prompt
    from real_estate.graphflow.writer import (
        AXIS_FOCUS,
        AXIS_TITLES,
        SectionDraft,
        Writer,
    )

    with pytest.raises(_Captured) as ei:
        Writer().write("tenant", "CARD-Y", [], outline=["p1", "p2"], violations=["v1"])
    cap = ei.value

    assert cap.schema is SectionDraft
    exp_system = korean_system_prompt(
        get_prompt("writer.role", title=AXIS_TITLES["tenant"],
                   focus=AXIS_FOCUS["tenant"])) + get_prompt("writer.rules")
    assert cap.system == exp_system
    exp_user = "\n\n".join([
        "[평가 대상 자산]\nCARD-Y",
        f"[증거 — 그래프 walk 수집, 경로 포함]\n{format_evidence([])}",
        "위 증거만으로 섹션을 작성하세요. 증거가 부족한 논점은 부족하다고 명시하세요.",
        "[아웃라인 — 이 논점들을 순서대로 다루며 구성하세요. 단 각 논점은 위 "
        "증거가 뒷받침하는 범위에서만 — 증거 없는 논점은 '근거 부족'으로 명시]\n- p1\n- p2",
        "[재작성 — 직전 출력의 위반 사항을 반드시 수정]\n- v1",
    ])
    assert cap.user == exp_user
