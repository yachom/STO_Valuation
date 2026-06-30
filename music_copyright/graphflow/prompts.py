"""music_copyright graphflow 프롬프트 — config/music_prompts.json 단일 출처.

2026-06-29: 프롬프트를 config/music_prompts.json로 집약했다(단일 source of truth).
모든 에이전트 프롬프트는 그 파일에서 편집한다. 코드의 _DEFAULTS는 빈 폴백이며,
get_prompt 우선순위는 active(실험 A/B) > config/music_prompts.json > _DEFAULTS({}).
에이전트는 get_prompt(key)만 호출한다. 로딩 메커니즘은 infra.config_loader에 위임한다 —
슬롯 'music_prompts'(real_estate의 'prompts'와 분리, 무간섭).

⚠️ 새 프롬프트는 config/music_prompts.json에 추가한다(여기 _DEFAULTS가 아니라). 과거엔
   _DEFAULTS가 진실이고 config가 선택적 override였는데, 그 이중 구조가 stale 섀도잉을
   낳았다(코드에서 프롬프트를 고쳐도 config의 옛 값이 런타임을 덮어씀). 이제 config가
   유일한 진실이다.

사용:
    from music_copyright.graphflow.prompts import get_prompt
    system = get_prompt("music_navigator.system")
    frame  = get_prompt("base.frame", role=role_description)   # {role} 치환
"""

from __future__ import annotations

import logging

from infra.config_loader import get_layered
from infra.config_loader import reset as _loader_reset
from infra.config_loader import set_active as _loader_set_active

logger = logging.getLogger("music_copyright.prompts")

# 프롬프트 단일 출처는 config/music_prompts.json. 이 _DEFAULTS는 그 파일이 없을 때를 위한
# 빈 폴백일 뿐이다 — 키가 없으면 get_prompt가 KeyError로 알린다(config에 추가하라는 신호).
_DEFAULTS: dict[str, str] = {}


def get_prompt(key: str, **fmt) -> str:
    """프롬프트 문자열. 우선순위(높은 순): active > config/music_prompts.json > _DEFAULTS({}).

    fmt가 주어지면 .format(**fmt)로 {placeholder} 치환. 키가 없으면 KeyError.
    """
    merged = get_layered("music_prompts", "music_prompts.json", _DEFAULTS)
    text = merged.get(key)
    if text is None:
        raise KeyError(
            f"unknown prompt key: {key!r} (config/music_prompts.json에 등록 필요)")
    return text.format(**fmt) if fmt else text


def set_active_override(variant: dict[str, str] | None) -> None:
    """실험 러너용 — 이후 모든 get_prompt()에 얹히는 variant. None이면 해제."""
    _loader_set_active("music_prompts", variant)


def reset_prompts() -> None:
    """테스트/재로딩용 — 파일 캐시 비움(active override는 유지)."""
    _loader_reset("music_prompts")
