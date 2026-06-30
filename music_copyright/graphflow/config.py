"""music_copyright graphflow 중앙 설정 — 튜닝값(검색·확장·축)을 한 곳에.

real_estate/graphflow/config.py를 미러링한다. 로딩 *메커니즘*은
infra.config_loader에 위임하고(자산 무관 공통), 그 위에 음악 도메인 *기본값*
(_DEFAULTS, 안전망)을 얹는다. infra 코드도 real_estate도 import하지 않는다 —
두 번째 자산이 같은 로더를 자기 슬롯·파일·기본값으로 재사용함을 보이는 합성 지점.

슬롯: 'music_graphflow', 파일: config/music_graphflow.json.
models는 공유다 — infra.config_loader.get_models()(config/models.json)를 그대로
읽는다. 음악 에이전트명도 그 agents 맵에 있어야 티어가 잡힌다(없으면 llm이 에러).

우선순위: 인자 overrides > active(실험) > config/music_graphflow.json+env > _DEFAULTS.

사용:
    from music_copyright.graphflow.config import get_config
    cfg = get_config()
    k = cfg["retrieval"]["evidence_k"]
"""

from __future__ import annotations

import logging
from typing import Any

from infra.config_loader import deep_merge, get_layered, get_models
from infra.config_loader import reset as _loader_reset
from infra.config_loader import set_active as _loader_set_active

logger = logging.getLogger("music_copyright.config")

# config/music_graphflow.json 부재/파손 시 폴백(파일이 진실, 이건 안전망).
# models 기본값은 real_estate와 공유 티어 + 음악 에이전트명만 덧댄다.
_DEFAULTS: dict[str, Any] = {
    "models": {
        "tiers": {
            "opus":     {"provider": "anthropic", "model": "claude-opus-4-8", "max_tokens": 8000},
            "sonnet":   {"provider": "anthropic", "model": "claude-sonnet-4-6", "max_tokens": 8000},
            "haiku":    {"provider": "anthropic", "model": "claude-haiku-4-5-20251001", "max_tokens": 4096},
            "gpt_mini": {"provider": "openai", "model": "gpt-4o-mini", "max_tokens": 4096},
        },
        # 음악 에이전트 → 티어. config/models.json의 agents가 진실(이 _DEFAULTS는 안전망).
        # 실제 매핑(models.json): 추출·계획·제안 계열은 gpt_mini(gpt-4o-mini)로 저비용,
        # 품질이 중요한 writer/synthesizer만 gpt_54(gpt-5.4)로 상향한 비용/품질 분리 구조.
        # 아래 dict는 파일 부재 시 폴백이라 보수적으로 gpt_mini로 둔다(파일이 우선).
        "agents": {
            "music_navigator": "gpt_mini", "music_harvester": "gpt_mini",
            "music_writer": "gpt_mini", "music_synthesizer": "gpt_mini",
            "music_perspectivist": "gpt_mini", "music_questioner": "gpt_mini",
            "music_researcher": "gpt_mini", "music_moderator": "gpt_mini",
            "music_outliner": "gpt_mini", "music_social_extractor": "gpt_mini",
            "music_graph_proposer": "gpt_mini", "music_reranker": "gpt_mini",
            "music_agency_classifier": "gpt_mini",
        },
    },
    # 보고서 기본 4축 — 자산가치 3축(influence·popularity·sustainability) + 사회적
    # 가치 1축(social_value). axes.BASE_AXES / profiles.REPORT_AXES와 일치.
    "axes": ["influence", "popularity", "sustainability", "social_value"],
    "retrieval": {
        "evidence_k": 12, "news_k": 2, "alpha": 0.6, "max_hops": 4,
        "semantic": True, "corroboration": True, "corrob_beta": 0.35, "fact_reserve": 0,
    },
    "expand": {
        "rounds": 2, "frontier_max": 12, "queries_per_node": 3,
        "total_query_budget": 100, "novelty_threshold": 0.1,
        "min_query_utility": 0.18, "max_poor_rounds": 2,
    },
    "consolidate": {
        "enabled": True, "mode": "rules_first", "min_conf": 0.6, "scope_hops": 4,
    },
    "agentic_graph": {
        "enabled": False, "mode": "proposal_first", "min_conf": 0.72,
        "scope_hops": 4, "max_entities": 16, "facts_per_entity": 3,
        "max_proposals": 8,
        "auto_commit_labels": ["Fandom", "SocialTheme", "Cause", "PublicUse"],
    },
    "qa": {
        "min_evidence": 2, "min_top_score": 0.12,
        "mini_expand": {"rounds": 1, "frontier_max": 2, "total_query_budget": 3},
        "post_write_gap_expand": {
            "enabled": True, "max_axes": 2, "rounds": 1,
            "frontier_max": 2, "total_query_budget": 3,
            "min_axis_facts": 4,
            "axis_overrides": {"social_value": {"rounds": 2, "total_query_budget": 8}},
        },
    },
    # 담론 큐레이션 — static=결정 경로 불변(real_estate와 동형). TODO(domain).
    "curation": {
        "mode": "static", "moderator_max_rounds": 2,
        "moderator_budget_per_round": 6, "moderator_max_questions": 3,
        "perspectives_max": 2, "turns_per_perspective": 2, "research_budget": 3,
        "total_research_query_budget": 24,
    },
}

# models는 infra가 직접(llm) 읽으므로 슬롯 분리; 나머지는 music_graphflow 튜닝 슬롯.
_MODEL_DEFAULTS: dict = _DEFAULTS["models"]
_TUNING_DEFAULTS: dict = {k: v for k, v in _DEFAULTS.items() if k != "models"}


def get_config(overrides: dict | None = None) -> dict:
    """병합된 설정 dict. 우선순위(높은 순): 인자 overrides > active > 파일+env > _DEFAULTS.

    models는 infra.get_models()(공유 config/models.json+env)에 음악 도메인 기본값을
    안전망으로 얹고, 나머지는 music_graphflow.json과 병합한다.
    """
    cfg = {
        "models": deep_merge(_MODEL_DEFAULTS, get_models()),
        **get_layered("music_graphflow", "music_graphflow.json", _TUNING_DEFAULTS),
    }
    if overrides:
        cfg = deep_merge(cfg, overrides)
    return cfg


def set_active_override(variant: dict | None) -> None:
    """실험 러너용 — 이후 모든 get_config()에 얹히는 variant. None이면 해제.

    variant의 'models' 부분은 공유 models 슬롯(llm도 즉시 반영), 나머지는
    music_graphflow 슬롯으로 간다. real_estate와 슬롯이 갈려 서로 간섭하지 않는다.
    """
    if variant is None:
        _loader_set_active("models", None)
        _loader_set_active("music_graphflow", None)
        return
    _loader_set_active("models", variant.get("models"))
    rest = {k: v for k, v in variant.items() if k != "models"}
    _loader_set_active("music_graphflow", rest or None)


def reset_config() -> None:
    """테스트/실험 재로딩용 — 캐시 비움(active override는 유지)."""
    _loader_reset("models")
    _loader_reset("music_graphflow")
