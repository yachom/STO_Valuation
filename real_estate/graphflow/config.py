"""graphflow 중앙 설정 — 튜닝 가능한 모든 값(모델·검색·확장)을 한 곳에.

비밀(API 키)은 .env에 남고, 여기엔 동작을 바꾸는 튜닝값만 둔다. 우선순위:
    env override (AGENT_TIER_<NAME>, TIER_<TIER>_*) > config.json > 코드 기본값

A/B 실험의 'variant' = 이 설정의 부분 override. 실험 러너가 config를 로드해
섹션을 덮어쓰고 파이프라인에 넘긴다 (load_config(overrides=...)).

사용:
    from real_estate.graphflow.config import get_config
    cfg = get_config()
    k = cfg["retrieval"]["evidence_k"]
    # 실험: cfg = get_config({"retrieval": {"corroboration": False}})
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("graphflow.config")

_PATH = Path(__file__).parent / "config.json"

# config.json 부재/파손 시 폴백 (파일이 진실, 이건 안전망).
_DEFAULTS: dict[str, Any] = {
    "models": {
        "tiers": {
            "opus":     {"provider": "anthropic", "model": "claude-opus-4-8", "max_tokens": 8000},
            "sonnet":   {"provider": "anthropic", "model": "claude-sonnet-4-6", "max_tokens": 8000},
            "haiku":    {"provider": "anthropic", "model": "claude-haiku-4-5-20251001", "max_tokens": 4096},
            "gpt_mini": {"provider": "openai", "model": "gpt-4o-mini", "max_tokens": 4096},
        },
        "agents": {
            "navigator": "haiku", "harvester": "gpt_mini", "prefilter": "gpt_mini",
            "curator": "haiku", "walk_judge": "haiku", "writer": "sonnet",
            "synthesizer": "opus", "auditor": "sonnet", "reviser": "sonnet",
            "qa": "sonnet", "qa_recognize": "haiku", "moderator": "haiku",
            "perspectivist": "sonnet", "questioner": "haiku", "researcher": "gpt_mini",
            "outliner": "sonnet",
        },
    },
    "retrieval": {
        "evidence_k": 12, "news_k": 2, "alpha": 0.6, "max_hops": 4,
        "semantic": True, "corroboration": True, "corrob_beta": 0.35, "fact_reserve": 0,
    },
    "expand": {
        "rounds": 2, "frontier_max": 12, "queries_per_node": 3,
        "total_query_budget": 100, "novelty_threshold": 0.1,
    },
    # 담론 큐레이션 (STORM/Co-STORM, D17~D22). static=기존 결정 경로 불변.
    "curation": {
        "mode": "static", "moderator_max_rounds": 2,
        "moderator_budget_per_round": 6, "moderator_max_questions": 3,
        "perspectives_max": 2, "turns_per_perspective": 2, "research_budget": 3,
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    """over를 base에 재귀 병합 (base 비파괴, 새 dict 반환)."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_file() -> dict:
    try:
        raw = json.loads(_PATH.read_text(encoding="utf-8"))
        raw.pop("_comment", None)
        return _deep_merge(_DEFAULTS, raw)
    except FileNotFoundError:
        logger.info("config.json 없음 — 코드 기본값 사용")
        return copy.deepcopy(_DEFAULTS)
    except (ValueError, OSError) as e:
        logger.warning("config.json 로드 실패(%s) — 기본값 사용", e)
        return copy.deepcopy(_DEFAULTS)


def _apply_env(cfg: dict) -> dict:
    """기존 env override 컨벤션 유지 (config가 .env보다 후순위).

    AGENT_TIER_<NAME>=<tier>, TIER_<TIER>_PROVIDER/MODEL/MAX_TOKENS/BASE_URL.
    """
    cfg = copy.deepcopy(cfg)
    agents = cfg["models"]["agents"]
    for name in list(agents):
        v = os.environ.get(f"AGENT_TIER_{name.upper()}")
        if v:
            agents[name] = v.lower()
    for tier, spec in cfg["models"]["tiers"].items():
        p = f"TIER_{tier.upper()}_"
        for envk, ck in (("PROVIDER", "provider"), ("MODEL", "model"), ("BASE_URL", "base_url")):
            val = os.environ.get(p + envk)
            if val:
                spec[ck] = val
        mt = os.environ.get(p + "MAX_TOKENS")
        if mt:
            spec["max_tokens"] = int(mt)
    return cfg


_cache: dict | None = None
_active: dict | None = None       # 실험 러너가 set한 variant override (전역 적용)


def get_config(overrides: dict | None = None) -> dict:
    """병합된 설정 dict. 우선순위(높은 순): 인자 overrides > active(실험) > 파일+env.

    compose·llm 등 모든 소비자가 이걸 읽으므로, 실험 러너가 set_active_override로
    variant를 걸면 파이프라인 전체가 그 설정으로 돈다(스레딩 불필요).
    """
    global _cache
    if _cache is None:
        _cache = _apply_env(_load_file())
    cfg = _cache
    if _active:
        cfg = _deep_merge(cfg, _active)
    if overrides:
        cfg = _deep_merge(cfg, overrides)
    return cfg


def set_active_override(variant: dict | None) -> None:
    """실험 러너용 — 이후 모든 get_config()에 얹히는 variant. None이면 해제.

    모델 변경 시 llm 캐시도 비워야 적용됨: llm.reset_caches() 동반 호출.
    """
    global _active
    _active = variant


def reset_config() -> None:
    """테스트/실험 재로딩용 — 캐시 비움(active override는 유지)."""
    global _cache
    _cache = None
