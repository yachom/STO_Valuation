"""BASE_LLM 매니저 — 에이전트의 단일 LLM 진입점.

에이전트는 `get_llm("agent_name").with_structured_output(...)`만 호출한다.
provider 분기 / Langfuse 콜백 / 로컬 LLMTraceCallback / 모델 spec은 전부 이
모듈 안에서 처리한다 — 에이전트는 langfuse나 ChatAnthropic을 직접 import하지
않는다 ([[feedback-base-llm-pattern]]).

설정 우선순위: .env override (TIER_<TIER>_*, AGENT_TIER_<NAME>) > 코드 기본값.
Provider: anthropic(기본) / ollama / openai_compat (로컬 모델 swap).
Langfuse: LANGFUSE_PUBLIC_KEY/SECRET_KEY 있으면 자동 활성, 없으면 silent skip.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel

logger = logging.getLogger("infra.agent.llm")


@dataclass
class ProviderSpec:
    provider: str                 # "anthropic" | "ollama" | "openai_compat"
    model: str
    max_tokens: int
    base_url: str | None = None
    extra: dict = field(default_factory=dict)


# 모델 티어 spec(tier→provider/model) + 에이전트→tier 매핑은 중앙 설정에서 온다
# (config/models.json). env override(TIER_*_*, AGENT_TIER_*)는 config_loader가
# 적용한다 → 여기선 읽기만. [[feedback-base-llm-pattern]]


def _load_tier_specs() -> dict[str, ProviderSpec]:
    from infra.config_loader import get_models
    tiers = get_models()["tiers"]      # config/models.json + env override 적용된 상태
    return {
        tier: ProviderSpec(
            provider=s["provider"], model=s["model"],
            max_tokens=int(s.get("max_tokens", 4096)),
            base_url=s.get("base_url"), extra=dict(s.get("extra", {})))
        for tier, s in tiers.items()
    }


def _load_agent_tier_map() -> dict[str, str]:
    from infra.config_loader import get_models
    return dict(get_models()["agents"])


def _build_chat_model(spec: ProviderSpec, callbacks: list,
                      name: str | None = None) -> BaseChatModel:
    # name은 Runnable의 run name → Langfuse GENERATION 이름이 된다. 에이전트명을
    # 넘기면 비용이 'ChatAnthropic' 한 덩어리가 아니라 에이전트별로 집계된다.
    if spec.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        kwargs: dict[str, Any] = {"model": spec.model, "max_tokens": spec.max_tokens,
                                  "callbacks": callbacks}
        if name:
            kwargs["name"] = name
        kwargs.update(spec.extra)
        return ChatAnthropic(**kwargs)
    if spec.provider == "ollama":
        try:
            from langchain_ollama import ChatOllama
        except ImportError as e:
            raise RuntimeError("provider 'ollama'엔 langchain-ollama 필요") from e
        kwargs = {"model": spec.model, "callbacks": callbacks}
        if name:
            kwargs["name"] = name
        if spec.base_url:
            kwargs["base_url"] = spec.base_url
        kwargs.update(spec.extra)
        return ChatOllama(**kwargs)
    if spec.provider == "openai":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as e:
            raise RuntimeError("provider 'openai'엔 langchain-openai 필요") from e
        kwargs = {"model": spec.model, "max_tokens": spec.max_tokens,
                  "callbacks": callbacks,
                  "api_key": os.environ.get("OPENAI_API_KEY")}
        if name:
            kwargs["name"] = name
        kwargs.update(spec.extra)
        return ChatOpenAI(**kwargs)
    if spec.provider == "openai_compat":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as e:
            raise RuntimeError("provider 'openai_compat'엔 langchain-openai 필요") from e
        kwargs = {"model": spec.model, "callbacks": callbacks, "max_tokens": spec.max_tokens,
                  "api_key": os.environ.get("LOCAL_LLM_API_KEY", "local")}
        if name:
            kwargs["name"] = name
        if spec.base_url:
            kwargs["base_url"] = spec.base_url
        kwargs.update(spec.extra)
        return ChatOpenAI(**kwargs)
    raise ValueError(f"unknown provider: {spec.provider}")


# ───────────────────────────────────────────────────────────
# Langfuse handler 싱글톤 (LLM 호출 자동 추적)
# ───────────────────────────────────────────────────────────
_langfuse_lock = threading.Lock()
_langfuse_handler: Any | None = None
_langfuse_inited = False


def get_langfuse_handler() -> Any | None:
    """Langfuse CallbackHandler 싱글톤. 키 없으면 None (silent)."""
    global _langfuse_handler, _langfuse_inited
    if _langfuse_inited:
        return _langfuse_handler
    with _langfuse_lock:
        if _langfuse_inited:
            return _langfuse_handler
        _langfuse_inited = True
        pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
        sk = os.environ.get("LANGFUSE_SECRET_KEY")
        host = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")
        if not (pk and sk):
            logger.info("Langfuse 키 없음 — 클라우드 트레이싱 비활성")
            _langfuse_handler = None
            return None
        try:
            from langfuse import Langfuse
            from langfuse.langchain import CallbackHandler
            Langfuse(public_key=pk, secret_key=sk,
                     host=host or "https://cloud.langfuse.com")
            _langfuse_handler = CallbackHandler()
            logger.info("Langfuse 활성 — host=%s", host or "https://cloud.langfuse.com")
        except Exception as exc:
            logger.warning("Langfuse 초기화 실패 — 로컬 트레이싱만: %s", exc)
            _langfuse_handler = None
        return _langfuse_handler


# ───────────────────────────────────────────────────────────
# 캐시 + 공개 API
# ───────────────────────────────────────────────────────────
_llm_cache: dict[tuple[str, str], BaseChatModel] = {}
_cache_lock = threading.Lock()


def _override_key(overrides: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(overrides.items()))


def _attach_callbacks() -> list:
    """Langfuse(클라우드, 선택) + LLMTraceCallback(로컬 사이드카, 필수)."""
    from infra.agent.tracing import LLMTraceCallback
    callbacks: list = []
    lf = get_langfuse_handler()
    if lf is not None:
        callbacks.append(lf)
    callbacks.append(LLMTraceCallback())
    return callbacks


def _spec_for_agent(agent_name: str, overrides: dict) -> ProviderSpec:
    tier = _load_agent_tier_map().get(agent_name)
    if tier is None:
        raise ValueError(
            f"unknown agent '{agent_name}'. "
            f"config/models.json의 agents에 등록 필요.")
    spec = _load_tier_specs()[tier]
    if overrides:
        return ProviderSpec(
            provider=spec.provider, model=spec.model,
            max_tokens=int(overrides.get("max_tokens", spec.max_tokens)),
            base_url=spec.base_url,
            extra={**spec.extra, **{k: v for k, v in overrides.items() if k != "max_tokens"}})
    return spec


def provider_for_agent(agent_name: str) -> str:
    """에이전트가 실제로 쓰는 provider (anthropic/openai/...).

    provider별 분기 판단용 — 예: Anthropic 전용 프롬프트 캐싱(cache_control)을
    적용할지. 캐시(_llm_cache)와 무관하게 가벼운 spec 조회만 한다.
    """
    return _spec_for_agent(agent_name, {}).provider


def get_llm(agent_name: str, **overrides) -> BaseChatModel:
    """에이전트별 BaseChatModel. 캐시됨, Langfuse+로컬 트레이싱 콜백 자동 부착.

    사용:
        llm = get_llm("navigator").with_structured_output(NavOutput)
        llm = get_llm("writer", max_tokens=4096).invoke(messages)
    """
    cache_key = (agent_name, _override_key(overrides))
    cached = _llm_cache.get(cache_key)
    if cached is not None:
        return cached
    with _cache_lock:
        cached = _llm_cache.get(cache_key)
        if cached is not None:
            return cached
        spec = _spec_for_agent(agent_name, overrides)
        llm = _build_chat_model(spec, _attach_callbacks(), name=agent_name)
        _llm_cache[cache_key] = llm
        logger.info("get_llm('%s') → %s/%s (max_tokens=%d)",
                    agent_name, spec.provider, spec.model, spec.max_tokens)
        return llm


# ───────────────────────────────────────────────────────────
# Prompt caching helpers (Anthropic ephemeral, 5분 TTL)
# ───────────────────────────────────────────────────────────
def cache_block(text: str) -> dict:
    """invariant 텍스트를 cache_control: ephemeral로 마킹한 content block.

    최소 토큰 미만(Sonnet/Opus ~1024)이면 silent skip.
    """
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def cached_system(text: str) -> list[dict]:
    """system 메시지용 list[content_block] 단축형."""
    return [cache_block(text)]


def system_content(provider: str, system: str):
    """provider별 프롬프트 캐싱 전략을 캡슐화 — system 메시지 content를 만든다.

    - anthropic: cache_control: ephemeral 블록으로 명시적 캐시 마킹.
    - openai / 그 외: 평문 문자열. OpenAI는 1024토큰↑ 공통 프리픽스를 자동
      캐싱하므로 마커가 불필요하고(시스템 메시지를 앞에 두는 것으로 충분),
      Anthropic 전용 cache_control 블록을 보내면 오히려 호출이 깨진다.

    새 provider의 캐싱 전략은 이 함수 한 곳만 확장하면 된다.
    """
    if provider == "anthropic":
        return cached_system(system)
    return system


def reset_caches() -> None:
    """테스트/환경변수 재로딩용 — 캐시 + Langfuse 싱글톤 초기화."""
    global _langfuse_handler, _langfuse_inited
    with _cache_lock:
        _llm_cache.clear()
    with _langfuse_lock:
        _langfuse_handler = None
        _langfuse_inited = False
