"""Auditor — 2층 검증 (설계 §10.2).

기계층 (LLM 없음, 오탐 없음):
    - factual claim의 CITES 존재 + 인덱스 유효
    - opinion claim의 derived_from 존재 + 인덱스 유효
    - 수치 대조 — claim 속 숫자가 인용 증거 텍스트에 실재하는가
    - 저신뢰(confidence<0.5) 증거 단독 인용 차단 (§9.3)
    - EXPIRED 증거 단독 인용 차단 (§12)
    위반 시 해당 Writer 재작성 (위반 목록 첨부).

LLM층 (Sonnet):
    - 축간 모순, 과대 해석, 빠진 반대 증거 → notes로 기록.
    (notes 기반 GAP→미니 EXPAND 재진입 루프는 후속 PR — PR-D는 기록까지.)
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from real_estate.graphflow.agents.base import BaseAgent
from real_estate.graphflow.prompts import get_prompt
from real_estate.graphflow.composer import Evidence
from real_estate.graphflow.synthesizer import OverviewDraft
from real_estate.graphflow.writer import SectionDraft

logger = logging.getLogger("graphflow.auditor")

_NUM_RE = re.compile(r"\d[\d,\.]*")
_LOW_CONFIDENCE = 0.5


def _numbers(text: str) -> list[str]:
    """텍스트에서 숫자 토큰 추출·정규화 (콤마 제거, 말미 구두점 제거)."""
    out = []
    for tok in _NUM_RE.findall(text or ""):
        norm = tok.replace(",", "").rstrip(".")
        if len(norm.replace(".", "")) >= 2:     # 한 자리 숫자는 노이즈라 제외
            out.append(norm)
    return out


def _pool(texts: list[str]) -> str:
    return " ".join(t.replace(",", "") for t in texts if t)


# ───────────────────────────────────────────────────────────
# 기계층
# ───────────────────────────────────────────────────────────
def audit_section(draft: SectionDraft, evidence: list[Evidence]) -> list[str]:
    """섹션 1개 기계 검증. 반환: 위반 목록 (비면 통과)."""
    violations: list[str] = []
    ev_by_index = {ev.index: ev for ev in evidence}

    for ci, cl in enumerate(draft.claims):
        tag = f"claim[{ci}] '{cl.text[:40]}…'"
        if cl.kind == "factual":
            if not cl.cites:
                violations.append(f"{tag}: factual인데 cites 없음 — 근거 E# 필수")
                continue
            cited = [ev_by_index.get(i) for i in cl.cites]
            missing = [i for i, ev in zip(cl.cites, cited) if ev is None]
            if missing:
                violations.append(f"{tag}: 존재하지 않는 evidence 번호 {missing}")
                continue
            cited = [ev for ev in cited if ev is not None]
            # 수치 대조
            pool = _pool([ev.text for ev in cited] + [ev.path_str for ev in cited]
                         + [ev.published_at or "" for ev in cited] + [cl.as_of or ""])
            bad_nums = [n for n in _numbers(cl.text) if n not in pool]
            if bad_nums:
                violations.append(
                    f"{tag}: 인용 증거에 없는 숫자 {bad_nums} — 증거의 값 그대로만 사용")
            # 저신뢰 단독 인용
            confs = [ev.confidence for ev in cited if ev.confidence is not None]
            if confs and all(c < _LOW_CONFIDENCE for c in confs):
                violations.append(
                    f"{tag}: confidence<{_LOW_CONFIDENCE} 증거만 인용 — factual 불가, "
                    f"'~라는 언급이 있다' 수준으로 완화 필요")
            # 만료 증거 단독 인용
            if cited and all(ev.validity_status == "EXPIRED" for ev in cited):
                violations.append(f"{tag}: 유효기간 경과(EXPIRED) 증거만 인용")
        else:  # opinion
            if not cl.derived_from:
                violations.append(f"{tag}: opinion인데 derived_from 없음")
            else:
                bad = [i for i in cl.derived_from
                       if not (0 <= i < len(draft.claims)) or i == ci]
                if bad:
                    violations.append(f"{tag}: derived_from 인덱스 무효 {bad}")

    # 블록-주장 매핑 검증
    for bi, b in enumerate(draft.blocks):
        bad = [i for i in b.claim_indexes if not (0 <= i < len(draft.claims))]
        if bad:
            violations.append(f"block[{bi}]: claim 인덱스 무효 {bad}")
    return violations


def audit_overview(draft: OverviewDraft, n_global_claims: int) -> list[str]:
    """Overview 기계 검증 — factual은 섹션 claim [C#] 인용 필수."""
    violations: list[str] = []
    for ci, cl in enumerate(draft.claims):
        tag = f"overview claim[{ci}] '{cl.text[:40]}…'"
        if cl.kind == "factual":
            if not cl.cites:
                violations.append(f"{tag}: factual인데 섹션 주장 인용(C#) 없음")
            else:
                bad = [c for c in cl.cites if not (1 <= c <= n_global_claims)]
                if bad:
                    violations.append(f"{tag}: 존재하지 않는 C번호 {bad}")
    return violations


# ───────────────────────────────────────────────────────────
# LLM층
# ───────────────────────────────────────────────────────────
class AuditIssue(BaseModel):
    severity: Literal["high", "medium", "low"]
    kind: Literal["contradiction", "overreach", "missing_counter", "other"]
    location: str = Field(description="섹션/주장 위치 (예: 'commercial blocks[1]')")
    description: str = Field(description="문제 설명 한두 문장")


class AuditNotes(BaseModel):
    overall_pass: bool = Field(description="high 이슈가 없으면 true")
    issues: list[AuditIssue]


class AuditorLLM(BaseAgent):
    name = "auditor"

    def review(self, report_text: str) -> AuditNotes:
        return self.complete(system=get_prompt("auditor.system"),
                             user=report_text, schema=AuditNotes)
