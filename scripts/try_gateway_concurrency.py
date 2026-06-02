"""Gateway 동시 호출 시뮬레이션.

여러 스레드가 동시에 같은(또는 다른) API를 게이트웨이에 두드릴 때, API별로
min_interval_seconds 만큼 순차화되는지 검증한다. 실제 외부 호출은 하지 않고
gateway.wait()만 호출한다 — 게이트 동작 자체의 검증이 목적.

사용:
    python -m scripts.try_gateway_concurrency                 # 기본 5스레드 × 4회씩
    python -m scripts.try_gateway_concurrency 8 5             # 8스레드 × 5회씩
"""

import logging
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from infra.gateway import get_default


def _call(api_name: str, idx: int, sink: list) -> None:
    t0 = time.monotonic()
    get_default().wait(api_name)
    t1 = time.monotonic()
    sink.append((api_name, idx, t0, t1))
    logging.getLogger("scripts.try_gateway").info(
        "%s #%d wait=%.3fs", api_name, idx, t1 - t0
    )


def run(n_threads: int, calls_per_api: int) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(threadName)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("infra.gateway").setLevel(logging.DEBUG)

    apis = ["naver_search", "naver_news_scraper"]
    sink: list = []

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=n_threads) as ex:
        for api in apis:
            for i in range(calls_per_api):
                ex.submit(_call, api, i, sink)
    elapsed = time.monotonic() - started

    print(f"\n=== 총 경과 {elapsed:.3f}s, 호출 {len(sink)}건 ===")

    by_api: dict[str, list[float]] = defaultdict(list)
    for api, _idx, _t0, t1 in sink:
        by_api[api].append(t1)

    for api, times in by_api.items():
        times.sort()
        gaps = [b - a for a, b in zip(times, times[1:])]
        gap_str = ", ".join(f"{g:.3f}" for g in gaps)
        print(f"[{api}] 게이트 통과 간격(s): {gap_str}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    c = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    run(n, c)
