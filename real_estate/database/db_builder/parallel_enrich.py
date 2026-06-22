"""ThreadPool 기반 행 enrich 루프 공통 엔진.

efficiency_pipeline._fill_jibun/_fill_bdmgt 와 gseed 쪽이 바이트 단위로 같던
"행 목록을 ThreadPool로 단건 호출 → 결과 있으면 update → 배치 커밋 → SIGINT 중단"
패턴을 한 함수로 모은다.

호출 측이 행에서 키워드를 뽑고(lookup_fn) 결과를 DB에 반영하는(update_fn)
방식만 다르므로 둘 다 콜러블로 주입한다. 동작·출력은 원본과 정확히 같다.

    rows         enrich 대상 행 시퀀스
    lookup_fn    r -> (row_id, value)   value=None 이면 miss. stop 체크는 호출 측
                 클로저가 한다 (원본 동작 보존).
    update_fn    (row_id, value) -> None   value 가 truthy일 때만 호출됨
    workers      ThreadPool worker 수
    batch_commit (db.commit 호출 빈도) — i % batch_commit == 0 마다 commit + 진행출력
    db           commit() 를 가진 DB 객체
    stop         {"v": bool} 가변 dict. SIGINT 핸들러가 v=True 로 바꾸면 루프 종료.
                 None 이면 새로 만들고 SIGINT 핸들러를 설치한다 (원본과 동일).

반환: (ok, miss) 카운트.
"""

import signal
from concurrent.futures import ThreadPoolExecutor


def parallel_enrich(rows, lookup_fn, update_fn, *, workers, batch_commit, db, stop=None):
    total = len(rows)
    if stop is None:
        stop = {"v": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(v=True) or print("\n중단 요청"))

    ok = miss = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (row_id, value) in enumerate(ex.map(lookup_fn, rows), 1):
            if value:
                update_fn(row_id, value)
                ok += 1
            else:
                miss += 1
            if i % batch_commit == 0:
                db.commit()
                print(f"    {i}/{total} | ok={ok} miss={miss}")
            if stop["v"]:
                break
    db.commit()
    print(f"  완료: ok={ok}, miss={miss}")
    return ok, miss
