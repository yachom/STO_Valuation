"""API 게이트웨이: 외부 호출 client가 통과하는 단일 관문.

여러 에이전트/호출자가 동시에 같은 외부 API를 두드릴 때, 호출자 측의 sleep만으로는
전역 호출 간격을 통제할 수 없다. 게이트웨이는 in-memory 상태로 API별 마지막 호출
시각을 추적해 최소 간격을 강제한다.

현재 단계 한계:
- 단일 프로세스 내 동기화만 지원 (threading.Lock). 다중 프로세스는 미지원.
- 최소 호출 간격(min_interval_seconds)만 구현. 동시성 제한/백오프/회로차단기 등은 미구현.

설정 파일: infra/gateway/rate_limits.json
키 네이밍은 external 로거 컨벤션과 동일 (provider 폴더명).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger("infra.gateway")

_DEFAULT_CONFIG_PATH = Path(__file__).parent / "rate_limits.json"


class APIGateway:
    def __init__(self, config_path: str | Path | None = None):
        path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        with open(path, encoding="utf-8") as f:
            self._config: dict[str, dict] = json.load(f)
        self._per_api_lock: dict[str, threading.Lock] = {
            name: threading.Lock() for name in self._config
        }
        self._last_called_at: dict[str, float] = {}

    def wait(self, api_name: str) -> None:
        cfg = self._config.get(api_name)
        if cfg is None:
            logger.debug("등록되지 않은 API, 게이트 통과: %s", api_name)
            return
        min_interval = float(cfg.get("min_interval_seconds", 0.0))
        if min_interval <= 0:
            return
        with self._per_api_lock[api_name]:
            last = self._last_called_at.get(api_name, 0.0)
            wait_s = (last + min_interval) - time.monotonic()
            if wait_s > 0:
                logger.debug("%s: %.3fs 대기", api_name, wait_s)
                time.sleep(wait_s)
            self._last_called_at[api_name] = time.monotonic()


_default_lock = threading.Lock()
_default_gateway: APIGateway | None = None


def get_default() -> APIGateway:
    global _default_gateway
    if _default_gateway is None:
        with _default_lock:
            if _default_gateway is None:
                _default_gateway = APIGateway()
    return _default_gateway
