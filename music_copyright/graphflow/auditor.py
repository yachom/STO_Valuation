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
    mapped: set[int] = set()
    for bi, block in enumerate(draft.blocks):
        if not block.markdown.strip():
            violations.append(f"block[{bi}] markdown 비어 있음")
        for idx in block.claim_indexes:
            if not (0 <= idx < n_claims):
                violations.append(
                    f"block[{bi}] claim_indexes {idx} 범위 밖(0~{n_claims - 1})")
            else:
                mapped.add(idx)
    for i, cl in enumerate(draft.claims):
        if not cl.text.strip():
            violations.append(f"claim[{i}] 빈 문장")
        if i not in mapped:
            violations.append(f"claim[{i}] 어떤 block에도 연결되지 않음")
        if cl.kind == "factual":
            if not cl.cites:
                violations.append(f"claim[{i}] factual인데 인용(cites) 없음: {cl.text[:40]}")
            for c in cl.cites:
                if c not in valid:
                    violations.append(f"claim[{i}] 잘못된 인용 E{c} (유효 범위 1~{n})")
        if cl.kind == "opinion":
            if not cl.derived_from:
                violations.append(f"claim[{i}] opinion인데 derived_from 없음")
            for j in cl.derived_from:
                if not (0 <= j < n_claims):
                    violations.append(f"claim[{i}] derived_from 인덱스 {j} 범위 밖(0~{n_claims - 1})")
    return violations


def audit_overview(overview, n_section_claims: int,
                   expected_axes: list[str] | None = None) -> list[str]:
    """Overview 기계 검증. n_section_claims=섹션 claim 총수([C#] 범위)."""
    violations: list[str] = []
    mapped: set[int] = set()
    n_claims = len(overview.claims)
    for bi, block in enumerate(overview.blocks):
        if not block.markdown.strip():
            violations.append(f"overview block[{bi}] markdown 비어 있음")
        for idx in block.claim_indexes:
            if not (0 <= idx < n_claims):
                violations.append(
                    f"overview block[{bi}] claim index {idx} 범위 밖(0~{n_claims - 1})")
            else:
                mapped.add(idx)
    for i, cl in enumerate(overview.claims):
        if not cl.text.strip():
            violations.append(f"overview claim[{i}] 빈 문장")
        if i not in mapped:
            violations.append(f"overview claim[{i}] 어떤 block에도 연결되지 않음")
        if cl.kind == "factual":
            if not cl.cites:
                violations.append(f"overview claim[{i}] factual인데 인용 없음")
            for c in cl.cites:
                if not (1 <= c <= n_section_claims):
                    violations.append(f"overview claim[{i}] 잘못된 인용 C{c} (1~{n_section_claims})")
    if expected_axes is not None:
        actual = [a.axis for a in overview.axis_scores]
        for axis in expected_axes:
            if actual.count(axis) != 1:
                violations.append(
                    f"axis_scores에 '{axis}'가 정확히 1개여야 함(현재 {actual.count(axis)}개)")
        extras = sorted(set(actual) - set(expected_axes))
        if extras:
            violations.append(f"axis_scores에 알 수 없는 축: {extras}")
    return violations
