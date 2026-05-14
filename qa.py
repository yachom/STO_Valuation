#!/usr/bin/env python3
"""QA CLI for STO valuation report.

Usage:
    python qa.py "ESG 점수가 왜 68이야?"
    python qa.py                       # REPL 모드
    python qa.py --report report.md --result result.json --kg data/knowledge_graph.json "..."
"""

from __future__ import annotations

import argparse
import sys

from src.observability import init_langfuse
from src.qa.graph import answer
from src.qa.session import QASession


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("question", nargs="?", help="질문. 비우면 REPL 모드")
    p.add_argument("--report", default="report_v2.md")
    p.add_argument("--result", default="result_v2.json")
    p.add_argument("--kg", default="data/knowledge_graph.json")
    p.add_argument("--verbose", action="store_true", help="라우팅/전문가 답변 모두 표시")
    return p.parse_args()


def _print_answer(result: dict, verbose: bool) -> None:
    if verbose:
        print("=" * 60)
        print(f"[router] {result['routing']}")
        print("-" * 60)
        for name, ans in result["specialist_answers"].items():
            print(f"\n[{name}]\n{ans}")
        print("\n" + "=" * 60)
    print("\n" + result["answer"] + "\n")


def main() -> int:
    args = parse_args()
    client = init_langfuse()
    if client:
        print("[info] Langfuse 트레이싱 활성화", file=sys.stderr)

    session = QASession.load(args.report, args.result, args.kg)
    print(f"[loaded] 자산: {session.address_label()}", file=sys.stderr)

    if args.question:
        result = answer(args.question, session)
        _print_answer(result, args.verbose)
        if client:
            try:
                client.flush()
            except Exception:
                pass
        return 0

    # REPL
    print("[REPL] 종료: Ctrl-D 또는 'exit'. /verbose 토글.", file=sys.stderr)
    verbose = args.verbose
    try:
        while True:
            try:
                q = input("\n> ").strip()
            except EOFError:
                break
            if not q:
                continue
            if q.lower() in ("exit", "quit", ":q"):
                break
            if q.startswith("/verbose"):
                verbose = not verbose
                print(f"[verbose={verbose}]")
                continue
            result = answer(q, session)
            _print_answer(result, verbose)
    finally:
        if client:
            try:
                client.flush()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
