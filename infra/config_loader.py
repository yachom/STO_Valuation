"""레이어드 설정 로딩 메커니즘 — 자산 무관 공통 계층.

설정 *내용*(에이전트 이름·프롬프트 문구 등 도메인 기본값)은 각 자산이 소유한다
(real_estate/graphflow/config.py·prompts.py의 _DEFAULTS). 이 모듈은 *메커니즘*만
제공한다: 루트 config/ JSON 로딩, 깊은 병합, env override, A/B active override, 캐시.

레이어 우선순위(높은 순): active(실험 variant) > 루트 config/<name>.json > 호출자 defaults.

사용자가 만지는 설정 파일은 레포 루트 `config/`에 노출된다:
    config/models.json    — LLM 티어·에이전트 매핑 (infra가 직접 읽음 — get_models)
    config/graphflow.json — 파이프라인 튜닝 (real_estate가 defaults와 병합)
    config/prompts.json   — 에이전트 프롬프트 (real_estate가 defaults와 병합)
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("infra.config")

# 레포 루트의 사용자 노출 설정 디렉터리. infra/config_loader.py → parent=infra → repo root.
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def deep_merge(base: dict, over: dict | None) -> dict:
    """over를 base에 재귀 병합 (base 비파괴, 새 dict 반환)."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_json(filename: str) -> dict:
    """config/<filename> 로드 (_comment 제거). 부재/파손 시 빈 dict — 파일이 진실, 호출자 defaults가 안전망."""
    path = CONFIG_DIR / filename
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("_comment", None)
        return raw
    except FileNotFoundError:
        logger.info("%s 없음 — 호출자 기본값 사용", path)
        return {}
    except (ValueError, OSError) as e:
        logger.warning("%s 로드 실패(%s) — 호출자 기본값 사용", path, e)
        return {}


def apply_tier_env(models: dict) -> dict:
    """모델 설정에 env override 적용 (config보다 env가 우선).

    AGENT_TIER_<NAME>=<tier>, TIER_<TIER>_PROVIDER/MODEL/MAX_TOKENS/BASE_URL.
    models = {"tiers": {...}, "agents": {...}} 형태를 받아 비파괴로 새 dict 반환.
    """
    models = copy.deepcopy(models)
    agents = models.get("agents", {})
    for name in list(agents):
        v = os.environ.get(f"AGENT_TIER_{name.upper()}")
        if v:
            agents[name] = v.lower()
    for tier, spec in models.get("tiers", {}).items():
        p = f"TIER_{tier.upper()}_"
        for envk, ck in (("PROVIDER", "provider"), ("MODEL", "model"), ("BASE_URL", "base_url")):
            val = os.environ.get(p + envk)
            if val:
                spec[ck] = val
        mt = os.environ.get(p + "MAX_TOKENS")
        if mt:
            spec["max_tokens"] = int(mt)
    return models


# ───────────────────────────────────────────────────────────
# A/B active override (슬롯별) + 캐시
# ───────────────────────────────────────────────────────────
_active: dict[str, dict] = {}            # slot → variant override (전역 적용)
_cache: dict[str, dict] = {}             # slot → 병합·env적용된 baseline (active 제외)


def set_active(slot: str, variant: dict | None) -> None:
    """실험 러너용 — slot('models'|'graphflow'|'prompts')에 얹히는 variant. None이면 해제."""
    if variant is None:
        _active.pop(slot, None)
    else:
        _active[slot] = variant


def reset(slot: str | None = None) -> None:
    """캐시 비움(active override는 유지). slot 지정 시 해당 슬롯만."""
    if slot is None:
        _cache.clear()
    else:
        _cache.pop(slot, None)


def get_layered(slot: str, filename: str, defaults: dict,
                env_apply: Callable[[dict], dict] | None = None) -> dict:
    """defaults에 config/<filename>을 얹고(+선택적 env), active override를 read-time 적용.

    baseline(defaults+file+env)은 slot 단위 캐시. active는 매 호출 병합 — set_active를
    로드 이후에 걸어도 즉시 반영된다.
    """
    base = _cache.get(slot)
    if base is None:
        base = deep_merge(defaults, load_json(filename))
        if env_apply is not None:
            base = env_apply(base)
        _cache[slot] = base
    active = _active.get(slot)
    return deep_merge(base, active) if active else base


def get_models() -> dict:
    """LLM 티어·에이전트 매핑. infra/agent/llm.py가 직접 사용(자산 비의존).

    config/models.json + env override(TIER_*/AGENT_TIER_*) + active. 도메인 기본값은
    두지 않는다 — 파일이 진실. 부재 시 빈 dict(llm이 명확히 에러).
    """
    return get_layered("models", "models.json", {}, env_apply=apply_tier_env)
