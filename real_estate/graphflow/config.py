"""graphflow 중앙 설정 — 튜닝 가능한 모든 값(모델·검색·확장)을 한 곳에.

사용자가 만지는 설정 파일은 레포 루트 `config/`에 노출된다(models.json·graphflow.json).
이 모듈은 그 위에 도메인 *기본값*(_DEFAULTS, 안전망)을 얹고, 로딩 *메커니즘*은
[[infra.config_loader]]에 위임한다 — 메커니즘은 자산 무관 공통 계층이라 infra에 둔다.

비밀(API 키)은 .env에 남고, 여기엔 동작을 바꾸는 튜닝값만 둔다. 우선순위:
    env override (AGENT_TIER_<NAME>, TIER_<TIER>_*) > config/*.json > 코드 _DEFAULTS

A/B 실험의 'variant' = 이 설정의 부분 override. set_active_override가 models 부분과
나머지(graphflow)를 infra 슬롯으로 갈라 적용한다 ([[feedback-base-llm-pattern]]).

사용:
    from real_estate.graphflow.config import get_config
    cfg = get_config()
    k = cfg["retrieval"]["evidence_k"]
    # 실험: cfg = get_config({"retrieval": {"corroboration": False}})
"""

from __future__ import annotations

import logging
from typing import Any

from infra.config_loader import deep_merge, get_layered, get_models
from infra.config_loader import reset as _loader_reset
from infra.config_loader import set_active as _loader_set_active

logger = logging.getLogger("graphflow.config")

# config/*.json 부재/파손 시 폴백 (파일이 진실, 이건 안전망).
_DEFAULTS: dict[str, Any] = {
    "models": {
        "tiers": {
            "opus":     {"provider": "anthropic", "model": "claude-opus-4-8", "max_tokens": 8000},
            "sonnet":   {"provider": "anthropic", "model": "claude-sonnet-4-6", "max_tokens": 8000},
            "haiku":    {"provider": "anthropic", "model": "claude-haiku-4-5-20251001", "max_tokens": 4096},
            "gpt_mini": {"provider": "openai", "model": "gpt-4o-mini", "max_tokens": 4096},
            # 로컬 vLLM(openai_compat) — base_url은 TIER_LOCAL_BASE_URL로 override.
            "local":    {"provider": "openai_compat", "model": "Qwen/Qwen2.5-7B-Instruct",
                         "base_url": "http://localhost:8000/v1", "max_tokens": 4096},
        },
        "agents": {
            "navigator": "haiku", "harvester": "gpt_mini", "prefilter": "gpt_mini",
            "curator": "haiku", "walk_judge": "haiku", "writer": "sonnet",
            "synthesizer": "opus", "auditor": "sonnet", "reviser": "sonnet",
            "qa": "sonnet", "qa_recognize": "haiku", "moderator": "haiku",
            "perspectivist": "sonnet", "questioner": "haiku", "researcher": "gpt_mini",
            "outliner": "sonnet", "doc_requester": "local",
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
    # QA 충분성 판정 임계 + 미니 EXPAND 축소 예산 (D7).
    "qa": {
        "min_evidence": 2, "min_top_score": 0.12,
        "mini_expand": {"rounds": 1, "frontier_max": 2, "total_query_budget": 3},
    },
    # 담론 큐레이션 (STORM/Co-STORM, D17~D22). static=기존 결정 경로 불변.
    "curation": {
        "mode": "static", "moderator_max_rounds": 2,
        "moderator_budget_per_round": 6, "moderator_max_questions": 3,
        "perspectives_max": 2, "turns_per_perspective": 2, "research_budget": 3,
    },
    # 선택적 PDF 입력 축(건축물대장 표제부). pdf_path가 없으면 doc_intake 노드가
    # no-op이라 주소-only 경로는 불변. interrupt=true면 추가 전유부 필요 시 HITL.
    "documents": {
        "enabled": True, "kinds": ["building_register"],
        "min_priority": "medium", "use_llm": True, "interrupt": True,
    },
}

# models는 infra가 직접(llm) 읽으므로 슬롯 분리; 나머지는 graphflow 튜닝 슬롯.
_MODEL_DEFAULTS: dict = _DEFAULTS["models"]
_TUNING_DEFAULTS: dict = {k: v for k, v in _DEFAULTS.items() if k != "models"}


def get_config(overrides: dict | None = None) -> dict:
    """병합된 설정 dict. 우선순위(높은 순): 인자 overrides > active(실험) > 파일+env > _DEFAULTS.

    compose·writer 등 모든 소비자가 이걸 읽는다. models는 infra.get_models()(config/
    models.json+env)에 도메인 기본값을 안전망으로 얹고, 나머지는 graphflow.json과 병합.
    """
    cfg = {
        "models": deep_merge(_MODEL_DEFAULTS, get_models()),
        **get_layered("graphflow", "graphflow.json", _TUNING_DEFAULTS),
    }
    if overrides:
        cfg = deep_merge(cfg, overrides)
    return cfg


def set_active_override(variant: dict | None) -> None:
    """실험 러너용 — 이후 모든 get_config()에 얹히는 variant. None이면 해제.

    variant의 'models' 부분은 models 슬롯(llm도 즉시 반영), 나머지는 graphflow 슬롯으로
    간다. 모델 변경 시 llm 캐시도 비워야 적용됨: llm.reset_caches() 동반 호출.
    """
    if variant is None:
        _loader_set_active("models", None)
        _loader_set_active("graphflow", None)
        return
    _loader_set_active("models", variant.get("models"))
    rest = {k: v for k, v in variant.items() if k != "models"}
    _loader_set_active("graphflow", rest or None)


def reset_config() -> None:
    """테스트/실험 재로딩용 — 캐시 비움(active override는 유지)."""
    _loader_reset("models")
    _loader_reset("graphflow")
