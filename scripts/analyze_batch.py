"""batch_run 결과의 cross-run 통합 분석 → markdown 보고서.

사용:
    python -m scripts.analyze_batch
    python -m scripts.analyze_batch --batch reports/_batch_index.json

입력: reports/_batch_index.json + 각 report_id의 trace.jsonl + 사이드카
출력: reports/_analysis_{ts}.md  (분석 보고서)
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from scripts.run_full_pipeline import DB_PATH

REPORTS_DIR = DB_PATH.parent / "reports"


def load_trace(report_id: str) -> list[dict]:
    p = REPORTS_DIR / report_id / "trace.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def load_payload(report_id: str, payload_path: str | None) -> dict | None:
    if not payload_path:
        return None
    p = REPORTS_DIR / report_id / payload_path
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _avg(xs: list[float]) -> float:
    return statistics.mean(xs) if xs else 0.0


def analyze(batch_index: dict) -> str:
    results = batch_index.get("results", [])
    lines: list[str] = []
    push = lines.append

    push(f"# Batch Analysis — {len(results)} runs "
         f"({batch_index.get('ok_count', 0)} OK)")
    push("")
    push(f"Total elapsed: {batch_index.get('total_elapsed_sec', '?')}s")
    push("")

    # 1) Run summary
    push("## Run summary")
    push("")
    push("| # | 주소 | status | sigungu | bd_mgt_sn | crit_iter | pass | elapsed |")
    push("|---|------|--------|---------|-----------|-----------|------|---------|")
    for r in results:
        push(f"| {r.get('idx','-')} | {r.get('address','')} | "
             f"{r.get('status','-')} | {r.get('sigungu','-')} | "
             f"{r.get('bd_mgt_sn') or '없음'} | "
             f"{r.get('critic_iter','-')} | {r.get('critic_pass','-')} | "
             f"{r.get('elapsed_sec','-')}s |")
    push("")

    # 2) Event 통계 + trace 로딩
    all_events_by_run: dict[str, list[dict]] = {}
    for r in results:
        rid = r.get("report_id")
        if rid:
            all_events_by_run[rid] = load_trace(rid)

    push("## Event counts per run")
    push("")
    push("| # | report_id | total | tool_call | llm_start | sql | error |")
    push("|---|-----------|-------|-----------|-----------|-----|-------|")
    for r in results:
        rid = r.get("report_id")
        if not rid:
            continue
        counts = Counter(e.get("event", "?") for e in all_events_by_run.get(rid, []))
        push(f"| {r.get('idx','-')} | {rid} | {sum(counts.values())} | "
             f"{counts.get('tool_call', 0)} | "
             f"{counts.get('llm_call_start', 0)} | "
             f"{counts.get('sql_query', 0)} | "
             f"{counts.get('error', 0)} |")
    push("")

    # 3) per-node 토큰 (pipeline_summary)
    push("## Tokens per node (across all runs)")
    push("")
    node_tokens: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for evts in all_events_by_run.values():
        for e in evts:
            if e.get("event") != "pipeline_summary":
                continue
            for node, kinds in (e.get("tokens_by_node") or {}).items():
                for k, v in kinds.items():
                    if isinstance(v, (int, float)):
                        node_tokens[node][k] += int(v)
    push("| node | input | output | cache_creation | cache_read |")
    push("|------|-------|--------|----------------|------------|")
    for node in sorted(node_tokens):
        ks = node_tokens[node]
        push(f"| {node} | {ks.get('input_tokens', 0)} | "
             f"{ks.get('output_tokens', 0)} | "
             f"{ks.get('cache_creation_input_tokens', 0)} | "
             f"{ks.get('cache_read_input_tokens', 0)} |")
    push("")

    # 4) Tool call breakdown (tool × status)
    push("## Tool call breakdown")
    push("")
    tool_stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for evts in all_events_by_run.values():
        for e in evts:
            if e.get("event") != "tool_call":
                continue
            tool_stats[e.get("tool", "?")][e.get("status", "?")] += 1
    push("| tool | ok | unknown | missing_args | error | total |")
    push("|------|-----|---------|--------------|-------|-------|")
    for tool in sorted(tool_stats):
        s = tool_stats[tool]
        total = sum(s.values())
        push(f"| {tool} | {s.get('ok', 0)} | {s.get('unknown', 0)} | "
             f"{s.get('missing_args', 0)} | {s.get('error', 0)} | {total} |")
    push("")

    # 5) SQL breakdown (source × row_count)
    push("## SQL query breakdown by source")
    push("")
    sql_by_source: dict[str, list[int]] = defaultdict(list)
    for evts in all_events_by_run.values():
        for e in evts:
            if e.get("event") != "sql_query":
                continue
            sql_by_source[e.get("source", "?")].append(e.get("row_count", 0))
    push("| source | n_queries | n_zero | %zero | avg_rows |")
    push("|--------|-----------|--------|-------|----------|")
    for src in sorted(sql_by_source):
        rcs = sql_by_source[src]
        n_zero = sum(1 for r in rcs if r == 0)
        pct = (100 * n_zero / len(rcs)) if rcs else 0
        push(f"| {src} | {len(rcs)} | {n_zero} | "
             f"{pct:.1f}% | {_avg(rcs):.1f} |")
    push("")

    # 6) 0-row 테이블 top — 데이터 수집 단서
    push("## Tables with frequent 0-row results (data gap signal)")
    push("")
    zero_table_counter: Counter[str] = Counter()
    table_sql_examples: dict[str, str] = {}
    for evts in all_events_by_run.values():
        for e in evts:
            if e.get("event") != "sql_query" or e.get("row_count", -1) != 0:
                continue
            sql = e.get("sql", "")
            m = re.search(r"FROM\s+([a-zA-Z_][\w]*)", sql, re.IGNORECASE)
            if m:
                tbl = m.group(1)
                zero_table_counter[tbl] += 1
                table_sql_examples.setdefault(tbl, sql[:150])
    push("| table | zero-row count | sample SQL |")
    push("|-------|----------------|------------|")
    for tbl, n in zero_table_counter.most_common(15):
        push(f"| {tbl} | {n} | `{table_sql_examples.get(tbl,'?')[:120]}` |")
    push("")

    # 7) Analyst confidence per axis
    push("## Analyst confidence per axis")
    push("")
    conf_by_axis: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for evts in all_events_by_run.values():
        for e in evts:
            if e.get("event") != "analyst_result" or e.get("status") != "ok":
                continue
            axis = e.get("axis", "?")
            cf = e.get("confidence_final")
            cs = e.get("confidence_step1")
            if cf is not None:
                conf_by_axis[axis].append((cs or 0, cf))
    push("| axis | n | avg_step1 | avg_final | min_final | max_final |")
    push("|------|---|-----------|-----------|-----------|-----------|")
    for axis in sorted(conf_by_axis):
        rows = conf_by_axis[axis]
        s1s = [r[0] for r in rows if r[0] is not None]
        fcs = [r[1] for r in rows if r[1] is not None]
        push(f"| {axis} | {len(rows)} | {_avg(s1s):.2f} | "
             f"{_avg(fcs):.2f} | "
             f"{min(fcs) if fcs else 0:.2f} | "
             f"{max(fcs) if fcs else 0:.2f} |")
    push("")

    # 8) Critic issues
    push("## Critic issues — kind × severity")
    push("")
    issue_grid: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for evts in all_events_by_run.values():
        for e in evts:
            if e.get("event") != "critic_result":
                continue
            for i in (e.get("issues_summary") or []):
                k = i.get("kind", "?")
                sev = (i.get("severity") or "").lower()
                issue_grid[k][sev] += 1
    push("| kind | high | medium | low |")
    push("|------|------|--------|-----|")
    for kind in sorted(issue_grid):
        s = issue_grid[kind]
        push(f"| {kind} | {s.get('high', 0)} | "
             f"{s.get('medium', 0)} | {s.get('low', 0)} |")
    push("")

    # 9) Retriever returned vs requested
    push("## Retriever returned vs requested k")
    push("")
    ret_dist: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for r in results:
        rid = r.get("report_id")
        if not rid:
            continue
        for e in all_events_by_run.get(rid, []):
            if e.get("event") != "tool_call":
                continue
            t = e.get("tool", "")
            if not t.startswith("retriever:"):
                continue
            pl = load_payload(rid, e.get("payload_path"))
            if not pl:
                continue
            args = pl.get("args") or {}
            res = pl.get("result") or {}
            req = args.get("k")
            ret = res.get("returned")
            if req is None or ret is None:
                continue
            ret_dist[t].append((int(req), int(ret)))
    push("| retriever | calls | avg_returned/k | min_returned |")
    push("|-----------|-------|----------------|--------------|")
    for t in sorted(ret_dist):
        rows = ret_dist[t]
        ratios = [(r / k if k else 0) for k, r in rows]
        push(f"| {t} | {len(rows)} | {_avg(ratios):.2f} | "
             f"{min(r for _, r in rows)} |")
    push("")

    # 10) Per-axis scout success
    push("## Scout breakdown by axis")
    push("")
    scout_per_axis: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for evts in all_events_by_run.values():
        for e in evts:
            t = e.get("tool", "")
            if e.get("event") != "tool_call" or not t.startswith("scout:"):
                continue
            axis = e.get("axis", "?")
            scout_per_axis[axis][e.get("status", "?")] += 1
    push("| axis | ok | unknown | missing_args | error |")
    push("|------|----|---------|--------------|-------|")
    for ax in sorted(scout_per_axis):
        s = scout_per_axis[ax]
        push(f"| {ax} | {s.get('ok', 0)} | {s.get('unknown', 0)} | "
             f"{s.get('missing_args', 0)} | {s.get('error', 0)} |")
    push("")

    # 11) 데이터 수집 제안 자동 도출
    push("## 📊 Data collection / code suggestions (auto-derived)")
    push("")
    suggestions: list[str] = []

    # 0-row 상위 테이블
    for tbl, n in zero_table_counter.most_common(5):
        suggestions.append(
            f"- **`{tbl}`** 가 {n}회 0행 반환 → "
            f"해당 테이블 적재 범위 점검 (자치구·기간 커버리지)"
        )

    # missing_args 자주 발생하는 scout
    miss_top: list[tuple[str, int]] = []
    for tool, s in tool_stats.items():
        if tool.startswith("scout:") and s.get("missing_args", 0) > 0:
            miss_top.append((tool, s["missing_args"]))
    for tool, n in sorted(miss_top, key=lambda x: -x[1])[:3]:
        suggestions.append(
            f"- `{tool}` 이 {n}회 missing_args → "
            f"인자 명세를 prompt에 더 강하게 노출 (분석가가 인자 환각)"
        )

    # retriever shortfall
    for t, rows in ret_dist.items():
        ratios = [r / k if k else 0 for k, r in rows]
        avg = _avg(ratios)
        if avg < 0.7:
            suggestions.append(
                f"- `{t}` 평균 returned/k={avg:.2f} → "
                f"해당 axis candidate 풀이 작음 (데이터 부족 or seed 정책 보강)"
            )

    # confidence 낮은 axis
    for axis, rows in conf_by_axis.items():
        fcs = [r[1] for r in rows]
        if fcs and _avg(fcs) < 0.6:
            suggestions.append(
                f"- axis=**{axis}** 평균 confidence_final={_avg(fcs):.2f} → "
                f"evidence 부족; 추가 데이터/도구 검토"
            )

    # critic high issue
    high_kinds = [(k, s.get("high", 0)) for k, s in issue_grid.items()
                  if s.get("high", 0) >= 2]
    for k, n in sorted(high_kinds, key=lambda x: -x[1])[:3]:
        suggestions.append(
            f"- critic kind=**{k}** high {n}건 → "
            f"해당 카테고리 가이드/프롬프트 강화 검토"
        )

    if suggestions:
        for s in suggestions:
            push(s)
    else:
        push("- (자동 추출된 제안 없음 — trace 패턴 양호)")
    push("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--batch", action="append", default=None,
        help="batch 인덱스 JSON (반복 가능). 미지정 시 _batch_index.json만.",
    )
    args = parser.parse_args()

    if not args.batch:
        args.batch = [str(REPORTS_DIR / "_batch_index.json")]

    batches = [json.loads(Path(p).read_text(encoding="utf-8"))
               for p in args.batch]
    combined = {
        "total_elapsed_sec": sum(b.get("total_elapsed_sec", 0) for b in batches),
        "address_count": sum(b.get("address_count", 0) for b in batches),
        "ok_count": sum(b.get("ok_count", 0) for b in batches),
        "results": [r for b in batches for r in b.get("results", [])],
    }

    md = analyze(combined)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = REPORTS_DIR / f"_analysis_{ts}.md"
    out_path.write_text(md, encoding="utf-8")
    print(md)
    print(f"\n저장: {out_path}")


if __name__ == "__main__":
    main()
