"""API 게이트웨이: 외부 호출 client가 통과하는 단일 관문.

여러 에이전트/호출자가 동시에 같은 외부 API를 두드릴 때, 호출자 측의 sleep만으로는
전역 호출 간격을 통제할 수 없다. 게이트웨이는 API별 다음 허용 시각을 추적해
최소 간격을 강제한다.

설계 v2 — 슬롯 분배 방식:
    각 호출자는 락을 짧게 잡고 자기 "다음 발사 슬롯"만 계산한 뒤 즉시 락을 해제한다.
    sleep은 락 밖에서 한다. 결과적으로 워커가 N개여도 슬롯이 0, +Δ, +2Δ, ...
    분배되어 각 워커가 sleep 동안 다른 워커가 진행할 수 있다.

    예: min_interval=0.05s, 8 worker가 동시에 들어오면 슬롯은 0.00, 0.05, 0.10, ...,
    0.35로 분배. 각 워커는 자기 슬롯까지 sleep 후 HTTP. HTTP 응답 시간(~200ms)
    동안 다른 워커도 슬롯 받아 진행 가능 → 처리량 ~5배 향상.

현재 단계 한계:
    - 단일 프로세스 내 동기화만 지원 (threading.Lock).
    - 최대 동시 호출 제한은 미지원 (필요해질 때 semaphore 추가).
    - 회로차단기/백오프 등 고급 기능은 미구현.

설정 파일: infra/gateway/rate_limits.json
키 네이밍은 external 로거 컨벤션과 동일 (provider 폴더명).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("infra.gateway")

_DEFAULT_CONFIG_PATH = Path(__file__).parent / "rate_limits.json"


class APIGateway:
    def __init__(self, config_path: str | Path | None = None):
        path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        with open(path, encoding="utf-8") as f:
            self._config: dict[str, dict] = json.load(f)
        # 한 락으로 모든 API의 slot 계산을 보호한다. 락은 산술 몇 줄만 잡고
        # 즉시 해제되므로 contention이 없다.
        self._slot_lock = threading.Lock()
        self._next_allowed_at: dict[str, float] = defaultdict(float)

    def wait(self, api_name: str) -> None:
        cfg = self._config.get(api_name)
        if cfg is None:
            logger.debug("등록되지 않은 API, 게이트 통과: %s", api_name)
            return
        min_interval = float(cfg.get("min_interval_seconds", 0.0))
        if min_interval <= 0:
            return

        # ── 슬롯 분배 (락 짧게 잡음) ─────────────────────────
        with self._slot_lock:
            now = time.monotonic()
            my_slot = max(now, self._next_allowed_at[api_name])
            self._next_allowed_at[api_name] = my_slot + min_interval

        # ── 락 밖에서 sleep — 다른 워커가 그 사이 슬롯 받기 가능 ──
        sleep_for = my_slot - time.monotonic()
        if sleep_for > 0:
            logger.debug("%s: %.3fs 대기", api_name, sleep_for)
            time.sleep(sleep_for)


_default_lock = threading.Lock()
_default_gateway: APIGateway | None = None


def get_default() -> APIGateway:
    global _default_gateway
    if _default_gateway is None:
        with _default_lock:
            if _default_gateway is None:
                _default_gateway = APIGateway()
    return _default_gateway
