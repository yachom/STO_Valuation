"""LLM 어댑터 — BASE_LLM 매니저.

에이전트는 `get_llm("agent_name").with_structured_output(...)` 또는
`.bind_tools(...)`만 호출하면 된다. Langfuse callback / LLMTraceCallback /
provider 분기 / 모델 spec은 이 모듈 내부에서만 처리한다. 에이전트는
langfuse나 ChatAnthropic 등 provider를 직접 import하지 않는다.

설정 우선순위:
    1. .env 환경변수 override (TIER_<TIER>_*, AGENT_TIER_<NAME>)
    2. 코드 기본값 (_TIER_SPECS_DEFAULT, AGENT_TO_TIER_DEFAULT)

Provider:
    - anthropic     (default; ANTHROPIC_API_KEY)
    - ollama        (lazy; langchain_ollama 필요; 로컬 모델 서버)
    - openai_compat (lazy; langchain_openai; vLLM/LM Studio 등 OpenAI 호환)

Langfuse:
    LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL 모두 있으면 자동 활성화.
    키 누락 시 silent skip — 개발 환경에서 모르고 실행해도 동작.

레퍼런스: [[tech_stack]], [[feedback-base-llm-pattern]], [[project-langfuse-tracing]]
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from langchain_core.language_models import BaseChatModel

logger = logging.getLogger("llm")


class ModelTier(str, Enum):
    """[Legacy] build_llm(ModelTier.X) 호환용 enum. 신규 코드는 get_llm("agent_name")."""
    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"


# ───────────────────────────────────────────────────────────
# Provider spec + 기본값
# ───────────────────────────────────────────────────────────
@dataclass
class ProviderSpec:
    provider: str          # "anthropic" | "ollama" | "openai_compat"
    model: str
    max_tokens: int
    base_url: str | None = None  # 로컬 서버 URL (ollama/openai_compat)
    extra: dict = field(default_factory=dict)


# Claude 4.X 단일 source of truth. 환경변수로 override 가능.
_TIER_SPECS_DEFAULT: dict[str, ProviderSpec] = {
    "opus":   ProviderSpec("anthropic", "claude-opus-4-7",            8000),
    "sonnet": ProviderSpec("anthropic", "claude-sonnet-4-6",          8000),
    "haiku":  ProviderSpec("anthropic", "claude-haiku-4-5-20251001",  2048),
}


# 에이전트 이름 → tier. 새 에이전트는 여기에 등록.
AGENT_TO_TIER_DEFAULT: dict[str, str] = {
    "planner":            "opus",
    "planner_v3":         "opus",
    "synthesizer":        "opus",
    "final_editor":       "opus",
    "tenant_analyst":     "sonnet",
    "esg_analyst":        "sonnet",
    "commercial_analyst": "sonnet",
    "risk_critic":        "sonnet",
    "reviser_v2":         "sonnet",
    "reviser":            "sonnet",   # v1 호환
    "reflection":         "sonnet",   # Reflexion verbal memo (graph_v3)
    "qa":                 "sonnet",
    "walk_confidence":    "haiku",    # PR-3 walk loop 신뢰도 체크
}


def _load_tier_specs() -> dict[str, ProviderSpec]:
    """기본 spec + 환경변수 override 머지.

    `TIER_SONNET_PROVIDER=ollama`, `TIER_SONNET_MODEL=llama3.1:70b`,
    `TIER_SONNET_BASE_URL=http://localhost:11434`, `TIER_SONNET_MAX_TOKENS=4096`
    형태로 tier 단위 override 가능 — 로컬 모델 스위치.
    """
    out: dict[str, ProviderSpec] = {}
    for tier, default in _TIER_SPECS_DEFAULT.items():
        env_prefix = f"TIER_{tier.upper()}_"
        provider = os.environ.get(env_prefix + "PROVIDER", default.provider)
        model = os.environ.get(env_prefix + "MODEL", default.model)
        max_tokens_str = os.environ.get(env_prefix + "MAX_TOKENS")
        max_tokens = int(max_tokens_str) if max_tokens_str else default.max_tokens
        base_url = os.environ.get(env_prefix + "BASE_URL") or default.base_url
        out[tier] = ProviderSpec(
            provider=provider, model=model, max_tokens=max_tokens,
            base_url=base_url, extra=dict(default.extra),
        )
    return out


def _load_agent_tier_map() -> dict[str, str]:
    """기본 매핑 + 환경변수 override. AGENT_TIER_TENANT_ANALYST=opus 등."""
    out = dict(AGENT_TO_TIER_DEFAULT)
    for agent in AGENT_TO_TIER_DEFAULT:
        env_key = f"AGENT_TIER_{agent.upper()}"
        if env_key in os.environ:
            out[agent] = os.environ[env_key].lower()
    return out


# ───────────────────────────────────────────────────────────
# Provider factory
# ───────────────────────────────────────────────────────────
def _build_chat_model(spec: ProviderSpec, callbacks: list) -> BaseChatModel:
    if spec.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        kwargs: dict[str, Any] = {
            "model": spec.model, "max_tokens": spec.max_tokens,
            "callbacks": callbacks,
        }
        kwargs.update(spec.extra)
        return ChatAnthropic(**kwargs)
    if spec.provider == "ollama":
        try:
            from langchain_ollama import ChatOllama
        except ImportError as e:
            raise RuntimeError(
                "Provider 'ollama'를 쓰려면 langchain-ollama 설치 필요: "
                "pip install langchain-ollama"
            ) from e
        kwargs = {"model": spec.model, "callbacks": callbacks}
        if spec.base_url:
            kwargs["base_url"] = spec.base_url
        kwargs.update(spec.extra)
        return ChatOllama(**kwargs)
    if spec.provider == "openai_compat":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as e:
            raise RuntimeError(
                "Provider 'openai_compat'를 쓰려면 langchain-openai 설치 필요: "
                "pip install langchain-openai"
            ) from e
        kwargs = {
            "model": spec.model, "callbacks": callbacks,
            "max_tokens": spec.max_tokens,
            "api_key": os.environ.get("LOCAL_LLM_API_KEY", "local"),
        }
        if spec.base_url:
            kwargs["base_url"] = spec.base_url
        kwargs.update(spec.extra)
        return ChatOpenAI(**kwargs)
    raise ValueError(f"unknown provider: {spec.provider}")


# ───────────────────────────────────────────────────────────
# Langfuse handler — 싱글톤
# ───────────────────────────────────────────────────────────
_langfuse_lock = threading.Lock()
_langfuse_handler: Any | None = None
_langfuse_inited = False


def get_langfuse_handler() -> Any | None:
    """Langfuse CallbackHandler 싱글톤. 키 누락 시 None (silent).

    LangGraph `app.invoke(config={"callbacks": [handler]})` 등에서도 사용.
    """
    global _langfuse_handler, _langfuse_inited
    if _langfuse_inited:
        return _langfuse_handler
    with _langfuse_lock:
        if _langfuse_inited:
            return _langfuse_handler
        _langfuse_inited = True
        pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
        sk = os.environ.get("LANGFUSE_SECRET_KEY")
        host = os.environ.get("LANGFUSE_BASE_URL")
        if not (pk and sk):
            logger.info("Langfuse 키 없음 — 클라우드 트레이싱 비활성")
            _langfuse_handler = None
            return None
        try:
            from langfuse import Langfuse
            from langfuse.langchain import CallbackHandler
            # Langfuse 클라이언트 초기화 (handler 내부에서도 자동 초기화하지만
            # 명시적으로 host를 박아두는 게 디버깅에 좋다)
            Langfuse(public_key=pk, secret_key=sk,
                     host=host or "https://cloud.langfuse.com")
            _langfuse_handler = CallbackHandler()
            logger.info("Langfuse 활성 — host=%s",
                        host or "https://cloud.langfuse.com")
        except Exception as exc:
            logger.warning("Langfuse 초기화 실패 — 로컬 트레이싱만 유지: %s", exc)
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
    """기본 callbacks: Langfuse (선택) + LLMTraceCallback (필수)."""
    from real_estate.report.tracing import LLMTraceCallback
    callbacks: list = []
    lf = get_langfuse_handler()
    if lf is not None:
        callbacks.append(lf)
    callbacks.append(LLMTraceCallback())
    return callbacks


def _spec_for_agent(agent_name: str, overrides: dict) -> ProviderSpec:
    agent_map = _load_agent_tier_map()
    tier = agent_map.get(agent_name)
    if tier is None:
        raise ValueError(
            f"unknown agent '{agent_name}'. "
            f"real_estate/report/llm.py:AGENT_TO_TIER_DEFAULT에 등록 필요."
        )
    spec = _load_tier_specs()[tier]
    if overrides:
        return ProviderSpec(
            provider=spec.provider, model=spec.model,
            max_tokens=int(overrides.get("max_tokens", spec.max_tokens)),
            base_url=spec.base_url,
            extra={**spec.extra,
                   **{k: v for k, v in overrides.items() if k != "max_tokens"}},
        )
    return spec


def get_llm(agent_name: str, **overrides) -> BaseChatModel:
    """에이전트별 BaseChatModel 인스턴스. 캐시됨, Langfuse/LLMTraceCallback 자동 부착.

    사용:
        llm = get_llm("planner").with_structured_output(PlannerOutput)
        llm = get_llm("tenant_analyst").bind_tools(walk_tools)
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
        callbacks = _attach_callbacks()
        llm = _build_chat_model(spec, callbacks)
        _llm_cache[cache_key] = llm
        logger.info("get_llm('%s') → %s/%s (max_tokens=%d)",
                    agent_name, spec.provider, spec.model, spec.max_tokens)
        return llm


def build_llm(tier: ModelTier, **overrides) -> BaseChatModel:
    """[Legacy] tier 기반 호환 wrapper. 신규 코드는 get_llm("agent_name") 사용.

    내부에서는 tier 이름을 별도 cache key로 처리해서 get_llm과 충돌 안 함.
    """
    pseudo_agent = f"_legacy_tier_{tier.value}"
    cache_key = (pseudo_agent, _override_key(overrides))
    cached = _llm_cache.get(cache_key)
    if cached is not None:
        return cached
    with _cache_lock:
        cached = _llm_cache.get(cache_key)
        if cached is not None:
            return cached
        spec = _load_tier_specs()[tier.value]
        if overrides:
            spec = ProviderSpec(
                provider=spec.provider, model=spec.model,
                max_tokens=int(overrides.get("max_tokens", spec.max_tokens)),
                base_url=spec.base_url,
                extra={**spec.extra,
                       **{k: v for k, v in overrides.items() if k != "max_tokens"}},
            )
        callbacks = _attach_callbacks()
        llm = _build_chat_model(spec, callbacks)
        _llm_cache[cache_key] = llm
        logger.info("build_llm(%s) → %s/%s [legacy]",
                    tier.value, spec.provider, spec.model)
        return llm


def model_id_for(tier: ModelTier) -> str:
    return _load_tier_specs()[tier.value].model


# ───────────────────────────────────────────────────────────
# Prompt caching helpers (Anthropic ephemeral)
# ───────────────────────────────────────────────────────────
def cache_block(text: str) -> dict:
    """invariant 텍스트를 cache_control: ephemeral로 마킹한 content block.

    사용:
        llm.invoke([
            {"role": "system", "content": [cache_block(_SYSTEM)]},
            {"role": "user", "content": [
                cache_block(invariant_prefix),  # asset_summary 등 변동 없는 부분
                {"type": "text", "text": variable_part},
            ]},
        ])

    Anthropic은 마지막 cache_control이 박힌 block까지를 캐시 prefix로 잡는다.
    5분 TTL(ephemeral). 최소 토큰 미만(Sonnet/Opus ~1024)이면 silent skip.
    """
    return {"type": "text", "text": text,
            "cache_control": {"type": "ephemeral"}}


def cached_system(text: str) -> list[dict]:
    """system 메시지용 list[content_block] 단축형."""
    return [cache_block(text)]


def reset_caches() -> None:
    """테스트/환경변수 재로딩용. 캐시 + Langfuse 싱글톤 초기화."""
    global _langfuse_handler, _langfuse_inited
    with _cache_lock:
        _llm_cache.clear()
    with _langfuse_lock:
        _langfuse_handler = None
        _langfuse_inited = False
