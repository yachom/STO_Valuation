"""노드 텍스트 카드 — 노드별 1~3문장 한국어 요약을 `card_text` 프로퍼티로 저장.

retriever/QA가 카드를 벡터 임베딩해 의미 검색 + LLM 컨텍스트 압축에 쓴다.
카드는 결정적 템플릿이라 LLM 호출 없이 재현 가능하다. None/누락 필드는 자연스럽게
생략한다. 카드 품질이 곧 QA 앵커 매칭 품질이므로 (설계 §5.6) 성의 있게 채운다.

각 builder는 properties dict를 받아 한국어 string을 반환한다.
"""

from __future__ import annotations

import logging

from real_estate.graphflow.graph.schema import NODE_LABELS

logger = logging.getLogger("graphflow.cards")


def _fmt_int(v) -> str | None:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return None


# ───────────────────────────────────────────────────────────
# 템플릿
# ───────────────────────────────────────────────────────────
def card_for_asset(p: dict) -> str:
    name = p.get("building_name") or p.get("road_address") or p.get("jibun_address") or "상가 건물"
    parts = [name]
    loc = p.get("road_address") or p.get("jibun_address")
    if loc and loc != name:
        parts.append(f"({loc})")
    tail = "는 평가 대상 상가 건물이다."
    sigungu = p.get("sigungu")
    if sigungu:
        tail = f"는 {sigungu} 소재 평가 대상 상가 건물이다."
    bd = p.get("bd_mgt_sn")
    if bd:
        tail += f" 건물관리번호 {bd}."
    return "".join(parts) + tail


def card_for_complex(p: dict) -> str:
    name = p.get("canonical_name", "이름 미상")
    parts = [name]
    if p.get("road_address"):
        parts.append(f"({p['road_address']})")
    parts.append("는 ")
    sub = []
    if p.get("completion_date"):
        sub.append(f"{p['completion_date']} 준공")
    hh = _fmt_int(p.get("household_count"))
    if hh:
        sub.append(f"{hh}세대 규모")
    parts.append(", ".join(sub) if sub else "정보 미확정")
    parts.append("의 아파트 단지이다.")
    return "".join(parts)


def card_for_building(p: dict) -> str:
    name = p.get("name") or p.get("building_name") or "건물명 미상"
    addr = p.get("jibun_addr") or p.get("addr") or ""
    head = f"{name} ({addr})" if addr else name
    # 건축HUB 표제부 보강 시 물리 스펙 노출 (보고서 ESG/물리 평가의 근거)
    spec = []
    if p.get("main_use"):
        spec.append(str(p["main_use"]))
    fa, fb = p.get("floors_above"), p.get("floors_below")
    if fa:
        spec.append(f"지상{fa}층" + (f"/지하{fb}층" if fb else ""))
    if p.get("total_area_sqm"):
        spec.append(f"연면적 {p['total_area_sqm']}㎡")
    if p.get("structure"):
        spec.append(str(p["structure"]))
    if p.get("use_district"):
        spec.append(str(p["use_district"]))
    if p.get("approved_at"):
        spec.append(f"{p['approved_at']} 사용승인")
    if p.get("energy_grade"):
        spec.append(f"에너지효율 {p['energy_grade']}등급")
    spec_s = " — " + ", ".join(spec) if spec else ""
    return f"{head}{spec_s}. 건물관리번호 {p.get('bdmgt_sn', '')}."


def _grade_bench(p: dict) -> str:
    """인증 등급의 자치구 내 순위 문장 — 'BEEC 1++ 양호' 대신 비교 grounding."""
    n = p.get("grade_peer_count")
    if not n:
        return ""
    better = p.get("grade_better_count", 0)
    if better == 0:
        return f" 자치구 인증건물 {n}건 중 최고 등급."
    return (f" 자치구 인증건물 {n}건 중 상위 {round(p.get('grade_rank_pct', 0) * 100)}%"
            f"(더 높은 등급 {better}건).")


def card_for_certification(p: dict) -> str:
    kind = p.get("kind", "")
    grade = p.get("grade", "")
    date = p.get("issue_date", "")
    if kind == "BEEC":
        extra = []
        if p.get("primary_energy_req") is not None:
            extra.append(f"1차에너지소요량 {p['primary_energy_req']}")
        if p.get("energy_reduce_rate") is not None:
            extra.append(f"에너지절감률 {p['energy_reduce_rate']}%")
        extra_s = " (" + ", ".join(extra) + ")" if extra else ""
        return f"BEEC 건물에너지효율 인증 — 등급 {grade}, 인증일 {date}{extra_s}." + _grade_bench(p)
    if kind == "GSEED":
        std = p.get("cert_standard", "")
        ct = p.get("cert_type", "")
        return f"G-SEED 녹색건축 {ct} — 등급 {grade}, 기준 {std}, 인증일 {date}." + _grade_bench(p)
    return f"인증 {kind} 등급 {grade} ({date})."


def card_for_commercial_area(p: dict) -> str:
    name = p.get("trdar_name", "상권명 미상")
    kind = p.get("trdar_se_name", "")
    area = p.get("area_sqm")
    area_s = f", 면적 {area:,.0f}㎡" if area else ""
    base = f"상권 {name} ({kind}){area_s}."
    extras = []
    fl = _fmt_int(p.get("latest_floating_total"))
    if fl:
        extras.append(f"분기 유동인구 {fl}명")
    sl = _fmt_int(p.get("latest_sales_total"))
    if sl:
        extras.append(f"추정매출 {sl}원")
    if extras:
        q = p.get("latest_quarter", "")
        base += f" 최근({q}) " + ", ".join(extras) + "."
    # 비교 grounding: 동일 자치구·상권구분 내 매출 순위 + 추세.
    rank, n = p.get("sales_rank"), p.get("sales_peer_count")
    if rank and n:
        base += f" 동일 자치구 {kind} {n}곳 중 매출 {rank}위(상위 {round(rank / n * 100)}%)."
    tp = p.get("sales_trend_pct")
    if tp is not None:
        base += f" 매출 추세({p.get('sales_trend_span', '')}) {'+' if tp >= 0 else ''}{tp}%."
    return base


def card_for_industry(p: dict) -> str:
    name = p.get("industry_name", "업종 미상")
    trdar = p.get("trdar_name", "")
    sales = _fmt_int(p.get("sales_amount"))
    where = f"{trdar} 상권" if trdar else "상권"
    if sales:
        share = p.get("sales_share")
        q = p.get("sales_quarter", "")
        qs = f"{q} 분기 " if q else ""
        share_s = (f", 상권 매출의 {share * 100:.1f}%"
                   if isinstance(share, (int, float)) else "")
        return f"{name} — {where} 매출 상위 업종 ({qs}추정매출 {sales}원{share_s})."
    return f"{name} — {where} 주요 업종."


def card_for_admin_dong(p: dict) -> str:
    name = p.get("adstrd_name") or ""
    base = f"행정동 {name} ({p.get('adstrd_cd', '')})."
    pop = _fmt_int(p.get("latest_resident_total"))
    if pop:
        base += f" 상주인구 {pop}명({p.get('latest_resident_quarter', '')})."
    return base


def card_for_legal_dong(p: dict) -> str:
    name = p.get("bname") or ""
    return f"법정동 {name} ({p.get('bcode', '')})."


def card_for_transit_station(p: dict) -> str:
    name = p.get("station_name", "역명 미상")
    line = p.get("line_name", "")
    line_s = f" ({line})" if line else ""
    return f"{name}{line_s} 지하철역."


def card_for_tenancy_event(p: dict) -> str:
    kind = p.get("kind", "")
    ym = p.get("deal_year_month", "")
    area = p.get("area_sqm")
    area_s = f"{area}m²" if area else ""
    if kind == "sale":
        amount = _fmt_int(p.get("amount"))
        amount_s = f"{amount}만원" if amount else "금액 미상"
        return f"매매 거래 — {ym} {area_s} {amount_s}."
    if kind == "rent":
        deposit = _fmt_int(p.get("deposit")) or "0"
        monthly = p.get("monthly_rent") or 0
        ct = p.get("contract_type", "")
        if monthly == 0:
            return f"{ct} 거래 — {ym} {area_s} 보증금 {deposit}만원."
        return f"{ct} 거래 — {ym} {area_s} 보증금 {deposit}만원 / 월세 {monthly:,}만원."
    return f"거래 이벤트 ({ym})."


def card_for_development_project(p: dict) -> str:
    name = p.get("name", "사업명 미상")
    district = p.get("district", "")
    cat = p.get("category", "")
    stage = p.get("stage", "")
    return f"{district} {name} — {cat} ({stage})."


# ── 확장 수집물 (Harvester) ──
def card_for_web_doc(p: dict) -> str:
    title = p.get("title", "제목 미상")
    press = p.get("press") or p.get("source") or ""
    date = (p.get("published_at") or "")[:10]
    meta = ", ".join(x for x in (press, date) if x)
    return f"뉴스: {title}" + (f" ({meta})." if meta else ".")


def card_for_fact(p: dict) -> str:
    # 카드 = 사실 문장 자체 (임베딩 의미 검색의 본체) + 시점 표기
    text = p.get("text", "")
    date = (p.get("published_at") or "")[:10]
    return f"{text}" + (f" ({date} 보도)" if date else "")


def card_for_entity(p: dict) -> str:
    name = p.get("name") or p.get("entity_id", "엔티티 미상")
    guess = p.get("guess_label")
    suffix = f" (추정 종류: {guess})" if guess else ""
    return f"{name} — 수집 문서에서 언급된 엔티티{suffix}."


# ── 보고서 grounding (Writer/Synthesizer) ──
def card_for_report(p: dict) -> str:
    return f"평가 보고서 {p.get('report_id', '')} ({p.get('created_at', '')[:10]})."


def card_for_report_section(p: dict) -> str:
    return f"보고서 섹션 [{p.get('axis', '')}] {p.get('title', '')}"


def card_for_claim(p: dict) -> str:
    # 카드 = 주장 문장 자체 (QA 앵커 매칭에서 보고서 결론이 잡히도록)
    text = p.get("text", "")
    as_of = f" (기준 {p['as_of']})" if p.get("as_of") else ""
    return f"{text}{as_of}"


def card_for_question(p: dict) -> str:
    return f"질문: {p.get('text', '')}"


def card_for_feedback(p: dict) -> str:
    return f"발행자 피드백 [{p.get('kind', '')}→{p.get('target_id', '')}]: {p.get('comment', '')}"


def card_for_assessment(p: dict) -> str:
    return f"평가 회신 ({p.get('stance', '')}): {p.get('rationale', '')[:120]}"


def card_for_tenant(p: dict) -> str:
    """상가 입점업체 카드 — 상호·업종·앵커등급 + 리뷰 신호(있으면)."""
    name = p.get("상호") or "업체"
    cats = [c for c in (p.get("카테고리") or "").split(" > ") if c]
    leaf = cats[-1] if cats else ""
    head = f"{name} — {leaf}" if leaf else name
    grade = p.get("앵커등급")
    if grade:
        head += f" (입점 {grade})"
    sig = []
    if p.get("평점"):
        sig.append(f"평점 {p['평점']}")
    if p.get("후기수"):
        sig.append(f"카카오후기 {p['후기수']}")
    if p.get("블로그리뷰수"):
        sig.append(f"블로그리뷰 {p['블로그리뷰수']}")
    return head + "." + (" " + ", ".join(sig) + "." if sig else "")


_BUILDERS = {
    NODE_LABELS.ASSET: card_for_asset,
    NODE_LABELS.COMPLEX: card_for_complex,
    NODE_LABELS.BUILDING: card_for_building,
    NODE_LABELS.CERTIFICATION: card_for_certification,
    NODE_LABELS.COMMERCIAL_AREA: card_for_commercial_area,
    NODE_LABELS.INDUSTRY: card_for_industry,
    NODE_LABELS.ADMIN_DONG: card_for_admin_dong,
    NODE_LABELS.LEGAL_DONG: card_for_legal_dong,
    NODE_LABELS.TRANSIT_STATION: card_for_transit_station,
    NODE_LABELS.TENANCY_EVENT: card_for_tenancy_event,
    NODE_LABELS.TENANT: card_for_tenant,
    NODE_LABELS.DEVELOPMENT_PROJECT: card_for_development_project,
    NODE_LABELS.WEB_DOC: card_for_web_doc,
    NODE_LABELS.FACT: card_for_fact,
    NODE_LABELS.ENTITY: card_for_entity,
    NODE_LABELS.REPORT: card_for_report,
    NODE_LABELS.REPORT_SECTION: card_for_report_section,
    NODE_LABELS.CLAIM: card_for_claim,
    NODE_LABELS.QUESTION: card_for_question,
    NODE_LABELS.FEEDBACK: card_for_feedback,
    NODE_LABELS.ASSESSMENT: card_for_assessment,
}


def build_card(label: str, props: dict) -> str:
    """라벨+프로퍼티로 카드 텍스트 생성. 빌더 미정의 라벨은 빈 문자열."""
    builder = _BUILDERS.get(label)
    if builder is None:
        logger.warning("카드 빌더 미정의: %s", label)
        return ""
    return builder(props)
