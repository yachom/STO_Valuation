"""특성화(안전망) — 리팩토링이 두 자산의 파이프라인 조립·에이전트 계층을 깨지 않게 잠근다.

Phase A(수집 커넥터 재배치)·Phase B(공통 에이전트 infra 역할베이스)가 import 경로와
에이전트 상속을 건드린다. 그 전에 다음을 baseline으로 고정한다:

  1. 두 자산의 ``build_report_graph()`` 가 컴파일된다(LangGraph 토폴로지 무결).
  2. 양 자산 공통 9역할 에이전트가 인스턴스화되고, ``BaseAgent`` 를 상속하며,
     기대한 ``name``(= 모델 티어·프롬프트 슬롯 키)을 갖는다.

DB·LLM 불필요 — 조립과 인스턴스화만 본다(네트워크 호출 없음). Phase B가 역할베이스로
공통 로직을 끌어올린 뒤에도 이 name/상속/조립은 그대로여야 한다. (프롬프트 동등성은
각 역할 전환 시점에 별도 골든 캡처로 검증한다 — 본 모듈 범위 밖.)
"""

from __future__ import annotations

import importlib

import pytest

from infra.agent.base import BaseAgent

# (자산, 모듈, 클래스, 기대 name) — 양 자산에 동시에 존재하는 공통 역할만.
COMMON_AGENTS = [
    # ── music_copyright ──
    ("music", "music_copyright.graphflow.navigator", "Navigator", "music_navigator"),
    ("music", "music_copyright.graphflow.harvester", "Harvester", "music_harvester"),
    ("music", "music_copyright.graphflow.discourse", "Perspectivist", "music_perspectivist"),
    ("music", "music_copyright.graphflow.discourse", "Questioner", "music_questioner"),
    ("music", "music_copyright.graphflow.discourse", "Researcher", "music_researcher"),
    ("music", "music_copyright.graphflow.discourse", "Outliner", "music_outliner"),
    ("music", "music_copyright.graphflow.moderator", "Moderator", "music_moderator"),
    ("music", "music_copyright.graphflow.synthesizer", "MusicSynthesizer", "music_synthesizer"),
    ("music", "music_copyright.graphflow.writer", "MusicWriter", "music_writer"),
    # ── real_estate ──
    ("re", "real_estate.graphflow.navigator", "Navigator", "navigator"),
    ("re", "real_estate.graphflow.harvester", "Harvester", "harvester"),
    ("re", "real_estate.graphflow.discourse", "Perspectivist", "perspectivist"),
    ("re", "real_estate.graphflow.discourse", "Questioner", "questioner"),
    ("re", "real_estate.graphflow.discourse", "Researcher", "researcher"),
    ("re", "real_estate.graphflow.discourse", "Outliner", "outliner"),
    ("re", "real_estate.graphflow.moderator", "Moderator", "moderator"),
    ("re", "real_estate.graphflow.synthesizer", "Synthesizer", "synthesizer"),
    ("re", "real_estate.graphflow.writer", "Writer", "writer"),
]


@pytest.mark.parametrize(
    "asset,module,cls_name,agent_name",
    COMMON_AGENTS,
    ids=[f"{a[0]}:{a[2]}" for a in COMMON_AGENTS],
)
def test_common_agent_instantiates(asset, module, cls_name, agent_name):
    """공통 역할 에이전트는 BaseAgent를 상속하고 안정적인 name을 갖는다."""
    cls = getattr(importlib.import_module(module), cls_name)
    assert issubclass(cls, BaseAgent), f"{module}.{cls_name} must subclass BaseAgent"
    inst = cls()
    assert inst.name == agent_name, (
        f"{module}.{cls_name}.name = {inst.name!r}, expected {agent_name!r}"
    )


def test_music_report_graph_compiles():
    from music_copyright.graphflow.workflow import build_report_graph

    assert build_report_graph() is not None


def test_real_estate_report_graph_compiles():
    from real_estate.graphflow.workflow import build_report_graph

    assert build_report_graph() is not None
