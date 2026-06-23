"""Auditor (음악저작권) — 기계층 grounding 검증. real_estate 미러(경량판).

v1 스켈레톤은 기계층만:
    - factual claim은 cites([E#]) ≥1, 인용 번호가 실재 evidence 범위 내.
    - opinion claim의 derived_from 인덱스가 같은 섹션 claim 범위 내.
위반 문자열 목록을 반환 → Writer 재작성 1~2회로 수렴.

⚠️ TODO(후속): real_estate의 computations 재계산 검증 + LLM 감사층(모순·과대해석
   탐지) + Reviser는 이후 단계로(여기선 미구현).
"""

from __future__ import annotations


def audit_section(draft, evidence) -> list[str]:
    """섹션 1개 기계 검증. evidence는 compose 결과(1-base 인덱스)."""
    violations: list[str] = []
    n = len(evidence)
    valid = set(range(1, n + 1))
    n_claims = len(draft.claims)
    for i, cl in enumerate(draft.claims):
        if cl.kind == "factual":
            if not cl.cites:
                violations.append(f"claim[{i}] factual인데 인용(cites) 없음: {cl.text[:40]}")
            for c in cl.cites:
                if c not in valid:
                    violations.append(f"claim[{i}] 잘못된 인용 E{c} (유효 범위 1~{n})")
            if not cl.text.strip():
                violations.append(f"claim[{i}] 빈 문장")
        if cl.kind == "opinion":
            for j in cl.derived_from:
                if not (0 <= j < n_claims):
                    violations.append(f"claim[{i}] derived_from 인덱스 {j} 범위 밖(0~{n_claims - 1})")
    return violations


def audit_overview(overview, n_section_claims: int) -> list[str]:
    """Overview 기계 검증. n_section_claims=섹션 claim 총수([C#] 범위)."""
    violations: list[str] = []
    for i, cl in enumerate(overview.claims):
        if cl.kind == "factual":
            if not cl.cites:
                violations.append(f"overview claim[{i}] factual인데 인용 없음")
            for c in cl.cites:
                if not (1 <= c <= n_section_claims):
                    violations.append(f"overview claim[{i}] 잘못된 인용 C{c} (1~{n_section_claims})")
    return violations
