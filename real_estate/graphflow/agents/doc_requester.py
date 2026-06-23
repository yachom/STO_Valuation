"""DocRequester — 건축물대장 표제부를 읽고 '추가로 받아야 할 전유부'를 판정.

선택적 PDF 입력 축(D-doc). 표제부(집합건축물대장) PDF가 파싱되면, 이 에이전트가
변동사항·건축물 현황을 보고 '리스크가 있어 전유부(호별 대장)까지 확인해야 할 호수'를
정한다. 위반건축물·무단증축·대수선·용도변경 같은 신호가 있는 호는 호별 등기/대장으로
실사할 가치가 있다(due diligence).

프로젝트 패턴:
- 프로토타입(downloads/gov24/gov24_agent.py)의 Anthropic 직접호출(urllib + tool-use)을
  버리고 `BaseAgent` + `get_llm`/structured output 한 줄(self.complete)로 대체한다.
- 프롬프트 문구는 prompts.py(doc_requester.system) — 코드에 안 박는다.
- 모델 tier는 config/models.json의 agents['doc_requester'](기본 'local' = vLLM).
  env override AGENT_TIER_DOC_REQUESTER로 클라우드 폴백(예: sonnet).

LLM 키가 없거나 호출이 실패하면 결정적 keyword 휴리스틱(decide_with_heuristic)으로
폴백한다 — 오프라인/드라이런에서도 '무엇을 요청할지'가 항상 나온다(부재 데드락 회피).
호별 후속 요청은 사람에게 surface된다(HITL): gov.kr 발급 페이지 + 호수 목록.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import asdict, dataclass, field

from pydantic import BaseModel, Field

from real_estate.external.gov24.extract import ChangeEntry, Gov24Extract
from real_estate.external.gov24.parser import PdfText, squeeze_spaces
from real_estate.graphflow.agents.base import BaseAgent, coerce_json
from real_estate.graphflow.prompts import get_prompt

logger = logging.getLogger("graphflow.doc_requester")


# gov.kr 집합건축물대장(전유부) 발급 페이지 — 사람이 추가 PDF를 받을 곳(HITL 안내).
GOV24_ISSUE_URL = (
    "https://www.gov.kr/mw/AA020InfoCappView.do"
    "?CappBizCD=15000000098&HighCtgCD=A02004002&tp_seq=01&Mcode=10205"
)
PDF_KIND_JEONYU = "집합건축물대장(전유부)"


# ── LLM 비활성/실패 시 폴백 신호표(결정적). LLM 모드는 이 표에 의존하지 않고
#    변동사항/현황의 의미를 직접 읽는다. ────────────────────────────────────
FALLBACK_RISK_TERMS: dict[str, int] = {
    "위반건축물": 5, "무단": 5, "시정": 4, "불법": 5, "증축": 4, "대수선": 4,
    "방화구획": 4, "철거": 3, "용도변경": 4, "표시변경": 2, "직권정정": 1, "현황도": 1,
}
RISK_NEGATION_KEYWORDS = ("해제", "시정완료")
PRIORITY_RANK = {"low": 1, "medium": 2, "high": 3}

UNIT_RE = re.compile(r"(?P<prefix>[A-Z])?\s*(?P<number>\d{1,5})\s*호")
UNIT_RANGE_RE = re.compile(
    r"(?P<start_prefix>[A-Z])?\s*(?P<start>\d{1,5})\s*호\s*[~∼-]\s*"
    r"(?:(?P<end_prefix>[A-Z])?\s*)?(?P<end>\d{1,5})\s*호"
)


def _normalize_unit_text(text: str) -> str:
    return re.sub(
        r"\b([A-Z])\s*(\d{1,2})\s+(\d{2,3})\s*호",
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}호",
        text,
    )


def _normalize_unit(prefix: str | None, number: str) -> str:
    return f"{prefix or ''}{int(number)}호"


def unit_sort_key(unit_id: str) -> tuple[str, int, str]:
    m = re.match(r"(?P<prefix>[A-Z]?)(?P<number>\d+)", unit_id)
    if not m:
        return ("Z", 10**9, unit_id)
    return (m.group("prefix") or "", int(m.group("number")), unit_id)


def _expand_unit_ranges(text: str) -> set[str]:
    text = _normalize_unit_text(text)
    units: set[str] = set()
    for m in UNIT_RANGE_RE.finditer(text):
        start_prefix = m.group("start_prefix") or ""
        end_prefix = m.group("end_prefix") or start_prefix
        if start_prefix != end_prefix:
            continue
        start, end = int(m.group("start")), int(m.group("end"))
        if end < start or end - start > 80:
            continue
        for number in range(start, end + 1):
            units.add(_normalize_unit(start_prefix, str(number)))
    return units


def extract_units(text: str) -> set[str]:
    """텍스트에서 호수('101호', 'B102호', '101호~105호' 범위)를 뽑는다."""
    text = _normalize_unit_text(text)
    units = _expand_unit_ranges(text)
    for m in UNIT_RE.finditer(text):
        start = m.start()
        if start >= 1 and text[start - 1] == "제":
            continue
        if text[m.end(): m.end() + 2].startswith("서식"):
            continue
        units.add(_normalize_unit(m.group("prefix"), m.group("number")))
    return units


def _find_keywords(text: str) -> list[str]:
    return [k for k in FALLBACK_RISK_TERMS if k in text]


def _score_keywords(keywords: list[str], text: str) -> int:
    score = sum(FALLBACK_RISK_TERMS[k] for k in keywords)
    if any(k in text for k in RISK_NEGATION_KEYWORDS):
        score = max(1, score - 3)
    return score


def _priority_for_score(score: int) -> str:
    if score >= 8:
        return "high"
    if score >= 4:
        return "medium"
    return "low"


def make_request_id(extracted: Gov24Extract, unit_id: str) -> str:
    base = "|".join([extracted.building_id or "", extracted.unique_number or "",
                     extracted.name or "", unit_id])
    return f"gov24-unit-{hashlib.sha1(base.encode('utf-8')).hexdigest()[:12]}"


# ── 결과 모델 ───────────────────────────────────────────────────────────────
@dataclass
class DocRequest:
    """사람에게 surface되는 추가 문서 요청 1건(전유부 1호)."""
    request_id: str
    pdf_kind: str
    unit_id: str
    priority: str
    reason: str
    status: str = "pending"
    building_id: str | None = None
    unique_number: str | None = None
    building_name: str | None = None
    road_address: str | None = None
    issue_url: str = GOV24_ISSUE_URL


@dataclass
class DocRequesterResult:
    """doc_requester 1회 판정 결과(구조화). pending_doc_requests로 state에 흐른다."""
    source_pdf: str
    source_document_type: str | None
    building_id: str | None
    building_name: str | None
    decided_by: str                       # "llm" | "heuristic"
    needs_more_documents: bool
    requests: list[DocRequest] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict:
        d = asdict(self)
        return d


# ── LLM structured output 스키마 ─────────────────────────────────────────────
class UnitDecision(BaseModel):
    unit_id: str = Field(description="문서에 명시된 호수 그대로 (예: '405호', 'B101호')")
    should_request: bool = Field(description="이 호의 전유부 PDF를 추가로 받아야 하면 true")
    priority: str = Field(description="high | medium | low (실사 중요도)")
    reason: str = Field(description="요청/비요청 이유 한국어 한 문장")
    evidence_text: str = Field(default="", description="판단 근거가 된 문서 내 문구")

    @classmethod
    def _coerce(cls, v):
        return coerce_json(v)


class DocRequesterOutput(BaseModel):
    decisions: list[UnitDecision] = Field(
        default_factory=list,
        description="호별 추가 PDF 요청 판정. 리스크 호만 should_request=true.")
    notes: list[str] = Field(default_factory=list, description="짧은 한국어 메모")


def _compact_change_entries(extracted: Gov24Extract) -> list[dict]:
    return [
        {"page_index": c.page_index, "date": c.date, "description": c.description,
         "mentioned_units": sorted(extract_units(c.description), key=unit_sort_key)}
        for c in extracted.changes
    ]


def _units_from_changes(extracted: Gov24Extract) -> dict[str, list[str]]:
    units: dict[str, list[str]] = {}
    for c in extracted.changes:
        for u in extract_units(c.description):
            units.setdefault(u, []).append(f"p.{c.page_index} {c.date}: {c.description}")
    return units


# ── 결정적 폴백(LLM 없음/실패) ──────────────────────────────────────────────
def decide_with_heuristic(
    parsed: PdfText,
    extracted: Gov24Extract,
    *,
    min_priority: str = "medium",
) -> DocRequesterResult:
    """변동사항의 keyword 신호로 리스크 호를 추정 → 후속 전유부 요청 생성(결정적).

    LLM 키가 없거나 LLM 호출이 실패할 때의 안전망. 호별로 keyword 점수를 누적하고,
    부정어(해제/시정완료)는 점수를 깎는다.
    """
    min_rank = PRIORITY_RANK[min_priority]
    scores: dict[str, int] = {}
    reasons: dict[str, list[str]] = {}
    for c in extracted.changes:
        kws = _find_keywords(c.description)
        units = extract_units(c.description)
        if not kws or not units:
            continue
        sc = _score_keywords(kws, c.description)
        for u in units:
            scores[u] = scores.get(u, 0) + sc
            reasons.setdefault(u, [])
            for k in kws:
                tag = f"신호 '{k}'"
                if tag not in reasons[u]:
                    reasons[u].append(tag)

    requests: list[DocRequest] = []
    for unit_id in sorted(scores, key=lambda u: (-scores[u], unit_sort_key(u))):
        prio = _priority_for_score(scores[unit_id])
        if PRIORITY_RANK[prio] < min_rank:
            continue
        requests.append(DocRequest(
            request_id=make_request_id(extracted, unit_id),
            pdf_kind=PDF_KIND_JEONYU, unit_id=unit_id, priority=prio,
            reason="; ".join(reasons.get(unit_id, [])) or "변동사항 리스크 신호",
            building_id=extracted.building_id, unique_number=extracted.unique_number,
            building_name=extracted.name, road_address=extracted.road_address))

    notes = [f"keyword 휴리스틱 폴백 — priority>={min_priority} 호만 요청."]
    if not requests:
        notes.append("리스크 신호 호 없음 — 추가 전유부 요청 없음.")
    return DocRequesterResult(
        source_pdf=parsed.filename, source_document_type=extracted.document_type,
        building_id=extracted.building_id, building_name=extracted.name,
        decided_by="heuristic", needs_more_documents=bool(requests),
        requests=requests, notes=notes)


class DocRequester(BaseAgent):
    """표제부 → 추가 전유부 요청 판정 에이전트. LLM 1회 + keyword 폴백."""

    name = "doc_requester"

    def decide(
        self,
        parsed: PdfText,
        extracted: Gov24Extract,
        *,
        min_priority: str = "medium",
        use_llm: bool = True,
    ) -> DocRequesterResult:
        """추가 문서 필요 여부 + 호별 요청 목록. use_llm=False거나 실패 시 휴리스틱."""
        if not use_llm:
            return decide_with_heuristic(parsed, extracted, min_priority=min_priority)
        try:
            return self._decide_with_llm(parsed, extracted, min_priority=min_priority)
        except Exception as exc:   # 키 없음/네트워크/스키마 위반 → 결정적 폴백
            logger.warning("doc_requester LLM 실패 — keyword 휴리스틱 폴백: %s",
                           str(exc)[:160])
            res = decide_with_heuristic(parsed, extracted, min_priority=min_priority)
            res.notes.insert(0, f"LLM 판정 실패로 휴리스틱 사용: {str(exc)[:120]}")
            return res

    def _decide_with_llm(
        self,
        parsed: PdfText,
        extracted: Gov24Extract,
        *,
        min_priority: str,
    ) -> DocRequesterResult:
        units_ctx = _units_from_changes(extracted)
        units_block = "\n".join(
            f"- {u}: " + " / ".join(ctx[:2])
            for u, ctx in sorted(units_ctx.items(), key=lambda kv: unit_sort_key(kv[0]))
        ) or "(변동사항에서 식별된 호수 없음)"
        changes_block = "\n".join(
            f"- p{c['page_index']} {c['date']}: {c['description']}"
            for c in _compact_change_entries(extracted)
        ) or "(변동사항 없음)"
        clean_text = parsed.llm_text
        if len(clean_text) > 16000:
            clean_text = clean_text[:16000] + "\n\n[TRUNCATED]"

        user = (
            f"[문서 메타]\n"
            f"- 파일: {parsed.filename}\n"
            f"- 문서유형: {extracted.document_type}\n"
            f"- 건물명: {extracted.name} | 건물ID: {extracted.building_id}\n"
            f"- 도로명주소: {extracted.road_address}\n\n"
            f"[변동사항(원문)]\n{changes_block}\n\n"
            f"[변동사항에 언급된 호수와 맥락]\n{units_block}\n\n"
            f"[정리된 표제부 전문(참고)]\n{clean_text}\n\n"
            f"위 표제부를 검토해, 호별 전유부 PDF를 추가로 받아 실사할 가치가 있는 호를 "
            f"판정하세요. 리스크 호만 should_request=true."
        )
        out: DocRequesterOutput = self.complete(
            system=get_prompt("doc_requester.system"), user=user,
            schema=DocRequesterOutput)
        return self._to_result(parsed, extracted, out, min_priority=min_priority)

    @staticmethod
    def _to_result(
        parsed: PdfText,
        extracted: Gov24Extract,
        out: DocRequesterOutput,
        *,
        min_priority: str,
    ) -> DocRequesterResult:
        """LLM 출력 → DocRequesterResult. priority 게이트 + 중복 호 제거(코드 안전장치)."""
        min_rank = PRIORITY_RANK[min_priority]
        requests: list[DocRequest] = []
        seen: set[str] = set()
        for d in out.decisions:
            unit_id = squeeze_spaces(d.unit_id)
            if not unit_id or not d.should_request or unit_id in seen:
                continue
            prio = (d.priority or "medium").lower()
            if prio not in PRIORITY_RANK:
                prio = "medium"
            if PRIORITY_RANK[prio] < min_rank:
                continue
            seen.add(unit_id)
            requests.append(DocRequest(
                request_id=make_request_id(extracted, unit_id),
                pdf_kind=PDF_KIND_JEONYU, unit_id=unit_id, priority=prio,
                reason=squeeze_spaces(d.reason) or "LLM 추가 검토 권고",
                building_id=extracted.building_id,
                unique_number=extracted.unique_number,
                building_name=extracted.name, road_address=extracted.road_address))
        requests.sort(key=lambda r: (-PRIORITY_RANK[r.priority], unit_sort_key(r.unit_id)))
        notes = [squeeze_spaces(n) for n in out.notes if squeeze_spaces(n)]
        notes.append(f"LLM 판정 — priority>={min_priority} 호만 요청.")
        if not requests:
            notes.append("추가 전유부 요청 없음(리스크 호 미식별).")
        return DocRequesterResult(
            source_pdf=parsed.filename, source_document_type=extracted.document_type,
            building_id=extracted.building_id, building_name=extracted.name,
            decided_by="llm", needs_more_documents=bool(requests),
            requests=requests, notes=notes)


def format_human_doc_request(result: DocRequesterResult) -> dict:
    """추가 문서 요청을 사람이 읽을 구조화 payload로 (HITL surface).

    LangGraph interrupt()가 이 payload를 사람에게 던진다. gov.kr 발급 페이지 +
    어떤 표제부/전유부를(어느 호) 업로드해야 하는지 명시한다.
    """
    units = [r.unit_id for r in result.requests]
    msg = (
        f"건물 '{result.building_name or result.building_id}' 표제부를 검토한 결과, "
        f"아래 {len(units)}개 호에 위반·무단증축·대수선·용도변경 등 리스크 신호가 있어 "
        f"호별 전유부(집합건축물대장 전유부) PDF가 추가로 필요합니다.\n"
        f"요청 호수: {', '.join(units)}\n\n"
        f"[발급 방법] 정부24 '건축물대장 등·초본 발급' 페이지에서 위 호의 전유부를 "
        f"발급해 업로드해 주세요:\n{GOV24_ISSUE_URL}\n"
        f"(표제부는 이미 제출됨 — 위 호의 '전유부'만 받으면 됩니다.)"
    )
    return {
        "kind": "building_register_followup",
        "message": msg,
        "issue_url": GOV24_ISSUE_URL,
        "requested_units": units,
        "requests": [asdict(r) for r in result.requests],
        "building": {
            "building_id": result.building_id,
            "building_name": result.building_name,
        },
    }
