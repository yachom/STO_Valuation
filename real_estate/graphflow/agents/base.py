"""BaseAgent — 모든 graphflow 에이전트의 얇은 공통 베이스.

목적은 로깅/트레이싱 등 기초 공통 층의 1회 배선이다. 개별 에이전트는:
    - get_llm / langfuse / anthropic 을 직접 import하지 않는다.
    - self.complete(system=..., user=..., schema=...) 한 줄로 LLM을 호출한다.
    - 호출은 자동으로 (a) Langfuse agent span으로 그룹핑되고
      (b) 로컬 사이드카에 node_scope로 기록되며 (c) LLM 토큰이 roll-up 된다.

사용:
    class Navigator(BaseAgent):
        name = "navigator"
        def run(self, ...):
            return self.complete(system=..., user=..., schema=NavOutput)
"""

from __future__ import annotations

import logging

from real_estate.graphflow.llm import cached_system, get_llm
from real_estate.graphflow.tracing import node_scope, observe

logger = logging.getLogger("graphflow.agent")


def coerce_json(v):
    """structured output 방어 — 소형 모델이 list/dict 필드를 JSON 문자열로
    반환하는 경우가 있다 (Haiku에서 관측). field_validator(mode='before')에 사용:

        @field_validator("items", mode="before")
        @classmethod
        def _coerce(cls, v):
            return coerce_json(v)
    """
    if isinstance(v, str):
        import json
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return v
    return v


DOMAIN_HIERARCHY = """
도메인 위계 (명시적으로 적용 — 등급 순서 환각 차단):

[BEEC 건물에너지효율등급] 높음 → 낮음
  1+++등급 > 1++등급 > 1+등급 > 1등급 > 2등급 > 3등급 > 4등급 > 5등급 > 6등급 > 7등급
  ('+' 수가 많을수록 우수. '1'은 같은 base지만 plus가 우선.)

[G-SEED 녹색건축인증] 높음 → 낮음
  최우수(그린1등급) > 우수(그린2등급) > 우량(그린3등급) > 일반(그린4등급)
  (그린N의 N이 작을수록 우수. '우수'가 '우량'보다 높음을 혼동 금지.)
  '우량 이상' = 우량+우수+최우수. '우수 이상' = 우수+최우수.

[부동산 가치 vs 사회적 가치] 본 평가는 '사회적 가치' 중심.
  - 임차 사업자 안정성, 상권 건강도, 입지 접근성, 배후수요는 모두 사회적 가치 측면.
  - 단순 시세·매도가격은 평가의 부산물이며 핵심 결론이 아님.
"""


def korean_system_prompt(role_description: str) -> str:
    """상가 부동산 사회적 가치 평가 공통 시스템 프롬프트 + 역할 설명."""
    return (
        "당신은 한국 상가 부동산의 사회적 가치를 평가하는 보고서 에이전트입니다.\n"
        "평가 대상은 '상가 건물(주소 단위)'이며, 아파트 단지 데이터는 본 상가의 "
        "'배후수요(잠재 소비층)' 측정 컨텍스트로만 활용합니다.\n"
        f"이번 역할: {role_description}\n"
        "원칙:\n"
        "- 한국어로 답합니다.\n"
        "- 모든 주장은 제공된 evidence에서만 끌어옵니다. evidence 밖 사실은 추측하지 마세요.\n"
        "- 숫자는 evidence에 있는 값만 인용합니다.\n"
        "- 출처가 부족하면 그렇다고 명시하세요.\n"
        "- 도메인 위계(아래)를 무시하고 등급/순서를 임의로 추론하지 마세요.\n"
        + DOMAIN_HIERARCHY
    )


def format_paths(paths, *, max_paths: int | None = None) -> str:
    """EvidencePath list → LLM 컨텍스트 문자열. 각 경로에 [E#] 인덱스 부여.

    walk 결과(traversal.EvidencePath)를 그대로 받아 카드 체인을 직렬화한다.
    """
    items = paths if max_paths is None else paths[:max_paths]
    if not items:
        return "(관련 evidence 없음)"
    parts = []
    for i, p in enumerate(items, 1):
        body = "\n".join("   " + ln for ln in p.text().splitlines())
        parts.append(f"[E{i}] (score={p.score:.2f}, tail={p.tail.label}:{p.tail.key})\n{body}")
    return "\n\n".join(parts)


class BaseAgent:
    """LLM 에이전트 공통 베이스. 서브클래스는 클래스 변수 `name`을 설정한다."""

    name: str = ""
    as_type: str = "agent"          # Langfuse observation 종류

    def __init__(self, name: str | None = None):
        self.name = name or self.name
        if not self.name:
            raise ValueError("BaseAgent.name 미설정 — 서브클래스 클래스변수나 인자로 지정")
        # 인스턴스별 동적 이름으로 Langfuse span 데코레이터 준비
        self._observed = observe(name=self.name, as_type=self.as_type)

    def llm(self, **overrides):
        """이 에이전트의 BaseChatModel (tier는 llm.py AGENT_TO_TIER에서 결정)."""
        return get_llm(self.name, **overrides)

    def invoke(self, messages, *, schema=None, **overrides):
        """messages로 LLM 호출. node_scope(로컬) + observe(Langfuse) 래핑."""
        @self._observed
        def _call():
            model = self.llm(**overrides)
            if schema is not None:
                model = model.with_structured_output(schema)
            return model.invoke(messages)

        with node_scope(self.name):
            return _call()

    def complete(self, *, system: str, user, schema=None,
                 cache_system: bool = True, **overrides):
        """system+user 단발 호출 단축형. system은 기본적으로 prompt-cache 마킹.

        user는 str 또는 content block list. schema(Pydantic/JSON)면 structured output.
        """
        sys_content = cached_system(system) if cache_system else system
        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": user},
        ]
        return self.invoke(messages, schema=schema, **overrides)
