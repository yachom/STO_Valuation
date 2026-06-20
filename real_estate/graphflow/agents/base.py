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

import hashlib
import logging
import re

from real_estate.graphflow.llm import get_llm, provider_for_agent, system_content
from real_estate.graphflow.prompts import get_prompt
from infra.agent.tracing import node_scope, observe

logger = logging.getLogger("graphflow.agent")


_WS_RE = re.compile(r"\s+")


def normalize_text(s: str) -> str:
    """공백 제거 + 소문자 — 검색어/표면형(surface) 동일성 비교용 정규화.

    navigator(검색어 중복 판정)·harvester(mention surface 매칭)가 공유한다.
    """
    return _WS_RE.sub("", (s or "").strip().lower())


def short_hash(s: str, n: int = 16) -> str:
    """sha256 hexdigest 앞 n자 — 안정적 짧은 식별자(쿼리·팩트·문서·claim 키)."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:n]


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


# 프롬프트 문구는 prompts.py(레지스트리)가 단일 진실 — 코드에 박지 않는다.
# 외부 재노출(agents/__init__) 호환용 상수. 실제 조립은 korean_system_prompt가
# 매 호출 get_prompt로 하므로 A/B variant(active override)가 반영된다.
DOMAIN_HIERARCHY = get_prompt("base.domain_hierarchy")


def korean_system_prompt(role_description: str) -> str:
    """상가 부동산 사회적 가치 평가 공통 시스템 프롬프트 + 역할 설명."""
    return (get_prompt("base.frame", role=role_description)
            + get_prompt("base.domain_hierarchy"))


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

    def invoke(self, messages, *, schema=None, retries: int = 1, **overrides):
        """messages로 LLM 호출. node_scope(로컬) + observe(Langfuse) 래핑.

        structured output(schema) 호출은 모델이 간헐적으로 스키마 위반 출력을 내
        ValidationError로 run 전체가 깨질 수 있어 retries회 재시도한다(재호출 시
        보통 성공 — 확률적 실패). 비-schema 호출은 재시도 안 함.
        """
        @self._observed
        def _call():
            model = self.llm(**overrides)
            if schema is not None:
                model = model.with_structured_output(schema)
            return model.invoke(messages)

        with node_scope(self.name):
            attempts = retries + 1 if schema is not None else 1
            for i in range(attempts):
                try:
                    return _call()
                except Exception as exc:
                    if i + 1 >= attempts:
                        raise
                    logger.warning("%s 구조화출력 실패 (시도 %d/%d) — 재시도: %s",
                                   self.name, i + 1, attempts, str(exc)[:160])

    def complete(self, *, system: str, user, schema=None,
                 cache_system: bool = True, **overrides):
        """system+user 단발 호출 단축형. system은 기본적으로 prompt-cache 마킹.

        user는 str 또는 content block list. schema(Pydantic/JSON)면 structured output.

        프롬프트 캐싱 전략은 provider별로 다르다(llm.system_content가 캡슐화):
        Anthropic은 cache_control 블록, OpenAI는 평문(자동 캐싱). cache_system=False면
        provider 무관하게 평문.
        """
        sys_content = (system_content(provider_for_agent(self.name), system)
                       if cache_system else system)
        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": user},
        ]
        return self.invoke(messages, schema=schema, **overrides)
