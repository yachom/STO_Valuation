#!/usr/bin/env python3
"""STO Social Value Valuation - CLI entry.

Usage:
    python main.py "서울특별시 강남구 테헤란로 152"
    python main.py "..." --out report.md --json result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.graph import run as run_pipeline
from src.observability import init_langfuse


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("address", help="평가할 부동산 주소")
    p.add_argument("--out", default="report.md", help="마크다운 보고서 출력 경로")
    p.add_argument("--json", default="result.json", help="원천 결과 JSON 출력 경로")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    client = init_langfuse()
    if client:
        print("[info] Langfuse 트레이싱 활성화", file=sys.stderr)

    print(f"[run] address: {args.address}", file=sys.stderr)
    final = run_pipeline(args.address)

    report = final.get("report_markdown") or "(보고서 미생성)"
    Path(args.out).write_text(report, encoding="utf-8")

    Path(args.json).write_text(
        json.dumps(final, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    if client:
        try:
            client.flush()
        except Exception:
            pass

    print(f"[done] 보고서: {args.out} / 원천: {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
