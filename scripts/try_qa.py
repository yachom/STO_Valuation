"""합의된(final/qa_passed) 보고서에 대한 CLI QA — Streamlit 없이 빠른 점검용.

사용:
    python -m scripts.try_qa REPORT_ID "이 단지의 ESG 등급은?"
"""

import logging
import os
import sys
from pathlib import Path

from infra.env import load_env
from real_estate.report.agents.qa import answer_question
from real_estate.report.tool.retriever.report_scoped_retriever import ReportScopedRetriever
from real_estate.report.storage.report_repo import ReportRepository

DB_PATH = Path("real_estate/database/DB/real_estate.db")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY 환경변수 필요.")

    if len(sys.argv) < 3:
        sys.exit("사용: python -m scripts.try_qa REPORT_ID \"질문\"")
    report_id = sys.argv[1]
    question = " ".join(sys.argv[2:])

    repo = ReportRepository(DB_PATH)
    report = repo.fetch(report_id)
    if report is None:
        sys.exit(f"보고서 {report_id} 없음.")
    if report.status not in ("final", "qa_passed"):
        sys.exit(
            f"보고서 status={report.status} — QA는 final/qa_passed에서만. "
            "Review 탭에서 Final 전이 먼저."
        )

    retriever = ReportScopedRetriever(report)
    result = answer_question(question=question, retriever=retriever)

    print(f"\n=== 질문 ===\n{question}\n")
    print(f"=== 답변 (out_of_scope={result.out_of_scope}) ===")
    print(result.answer_markdown)
    if result.citations:
        print(f"\n=== 인용 ({len(result.citations)}개) ===")
        for c in result.citations:
            print(f"\n[E{c['e_index']}] {c.get('section_title')} ({c.get('axis')})")
            print(f"  {c['snippet_text'][:200]}")


if __name__ == "__main__":
    main()
