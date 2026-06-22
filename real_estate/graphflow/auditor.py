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

import ast
import logging
import operator as _op
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
# 파생수치 검증 (a-2) — LLM 산수는 믿지 않고 코드로 재계산
# ───────────────────────────────────────────────────────────
_SAFE_BINOPS = {ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul,
                ast.Div: _op.truediv, ast.Mod: _op.mod, ast.Pow: _op.pow}
_SAFE_UNARY = {ast.USub: _op.neg, ast.UAdd: _op.pos}


def _eval_ast(node):
    """숫자·산술연산자·괄호만 허용하는 AST 평가 (이름/호출/속성은 거부)."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("non-numeric constant")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_BINOPS:
        return _SAFE_BINOPS[type(node.op)](_eval_ast(node.left), _eval_ast(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_UNARY:
        return _SAFE_UNARY[type(node.op)](_eval_ast(node.operand))
    raise ValueError(f"unsupported node {type(node).__name__}")


def _safe_eval(expr: str) -> float | None:
    """산식 문자열을 안전 평가. 실패(미허용 토큰·0나눗셈 등) 시 None."""
    try:
        return float(_eval_ast(ast.parse(expr.replace(",", ""), mode="eval").body))
    except (ValueError, TypeError, SyntaxError, KeyError, ZeroDivisionError, OverflowError):
        return None


def _verify_computations(cl, pool: str, violations: list[str], tag: str) -> list[float]:
    """claim에 첨부된 산식을 코드로 재계산·검증. 통과한 result 리스트 반환.

    ① 입력 숫자가 인용 증거 pool에 실재 (환각 입력 차단)
    ② expression 평가값이 명시 result와 일치 (LLM 산수 오류 차단)
    둘 중 하나라도 실패하면 위반으로 기록하고 그 산식은 제외.
    """
    results: list[float] = []
    for comp in (getattr(cl, "computations", None) or []):
        # 입력 숫자는 인용 증거에 실재해야 함. 단 스케일 상수(백분율 100·억 1e8 등
        # 10의 거듭제곱)는 데이터가 아니라 단위 환산이므로 grounding 면제.
        bad = [n for n in _numbers(comp.expression)
               if n not in pool and not _is_free_constant(n)]
        if bad:
            violations.append(f"{tag}: 산식 입력 {bad}이 인용 증거에 없음 — 증거의 값만 산식에 사용")
            continue
        val = _safe_eval(comp.expression)
        if val is None:
            violations.append(
                f"{tag}: 산식 '{comp.expression[:40]}' 평가 불가 — 숫자·+−×÷·괄호만 허용")
            continue
        if abs(val - comp.result) > max(0.01, abs(val) * 0.01):
            violations.append(
                f"{tag}: 산식 결과 불일치 — {comp.expression}={val:.6g} ≠ 명시값 {comp.result}")
            continue
        results.append(comp.result)
    return results


_POW10_RE = re.compile(r"10+")


def _is_free_constant(num: str) -> bool:
    """산식의 스케일 상수(백분율 100·천만억 환산 등 10의 거듭제곱)는 grounding 면제.

    한 자리 상수(분기 4·평균 분모 2 등)는 _numbers가 애초에 추출하지 않으므로
    여기선 10·100·1000…만 면제하면 된다.
    """
    return bool(_POW10_RE.fullmatch(num))


def _is_derived(num: str, results: list[float]) -> bool:
    """본문 숫자가 검증된 산식 결과의 반올림 표현인가 (정수반올림 0.5 또는 5% 이내)."""
    try:
        x = float(num)
    except ValueError:
        return False
    return any(abs(x - r) <= max(0.5, abs(r) * 0.05) for r in results)


# ───────────────────────────────────────────────────────────
# 기계층
# ───────────────────────────────────────────────────────────
def audit_section(draft: SectionDraft, evidence: list[Evidence]) -> list[str]:
    """섹션 1개 기계 검증. 반환: 위반 목록 (비면 통과)."""
    violations: list[str] = []
    ev_by_index = {ev.index: ev for ev in evidence}
    # 섹션 전체 증거 pool — opinion 산식의 입력 숫자 grounding 대조용.
    section_pool = _pool([ev.text for ev in evidence] + [ev.path_str for ev in evidence]
                         + [ev.published_at or "" for ev in evidence])

    for ci, cl in enumerate(draft.claims):
        tag = f"claim[{ci}] '{cl.text[:40]}…'"
        if cl.kind == "gap":
            # 부재·유보 진술 — cites·derived_from 불요(가리킬 증거도, 도출 선행도 없음).
            # 단 수치가 있으면 '부재'와 모순 → 차단(환각 사실 밀반입 방지).
            if _numbers(cl.text):
                violations.append(
                    f"{tag}: gap(부재) 진술에 수치 포함 — 부재 주장과 모순이므로 수치를 빼거나 "
                    f"factual/opinion으로 전환")
            continue
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
            # 수치 대조 — 인용 증거에 그대로 있거나, 첨부 산식의 검증된 계산 결과여야 함.
            pool = _pool([ev.text for ev in cited] + [ev.path_str for ev in cited]
                         + [ev.published_at or "" for ev in cited] + [cl.as_of or ""])
            derived = _verify_computations(cl, pool, violations, tag)
            bad_nums = [n for n in _numbers(cl.text)
                        if n not in pool and not _is_derived(n, derived)]
            if bad_nums:
                violations.append(
                    f"{tag}: 인용 증거에 없는 숫자 {bad_nums} — 증거 값 그대로 쓰거나 "
                    f"computations에 산식 첨부")
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
            # opinion에 산식이 붙어 있으면 검증(LLM 산수 오류 차단) — 입력은 섹션 전체 증거 대상.
            _verify_computations(cl, section_pool, violations, tag)

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
    fix_target: str = Field(
        default="",
        description="수정 대상 블록 id — 본문 각 문단 앞 [#blk_...] 앵커 중 이 이슈를 "
                    "고치려면 어느 문단을 손봐야 하는지 가장 직접 관련된 하나. "
                    "요약/종합(overview) 문단이거나 특정 못하면 빈 문자열")
    description: str = Field(description="문제 설명 한두 문장")


class AuditNotes(BaseModel):
    overall_pass: bool = Field(description="high 이슈가 없으면 true")
    issues: list[AuditIssue]


class AuditorLLM(BaseAgent):
    name = "auditor"

    def review(self, report_text: str) -> AuditNotes:
        return self.complete(system=get_prompt("auditor.system"),
                             user=report_text, schema=AuditNotes)
