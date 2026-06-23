"""노드 텍스트 카드 (음악저작권) — 노드별 한국어 요약을 card_text로 저장.

real_estate/graphflow/graph/cards.py를 미러링한다. retriever/QA가 카드를 벡터
임베딩해 의미 검색 + LLM 컨텍스트 압축에 쓴다. 카드는 결정적 템플릿이라 LLM
호출 없이 재현 가능. None/누락 필드는 자연스럽게 생략한다.

라벨→빌더 디스패치 *메커니즘*은 infra.graph.cards.build_card에 위임한다 — 여기엔
음악 도메인 빌더 MAP만 둔다(두 번째 자산이 같은 레지스트리 메커니즘을 재사용함을
보이는 합성 지점).

⚠️ TODO(domain): 필드명/문구는 placeholder다. 도메인 확정 후 정련한다.
"""

from __future__ import annotations

import logging

from music_copyright.graphflow.graph.schema import NODE_LABELS

logger = logging.getLogger("music_copyright.cards")


def _fmt_int(v) -> str | None:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return None


# ───────────────────────────────────────────────────────────
# 템플릿 (placeholder)
# ───────────────────────────────────────────────────────────
def card_for_work(p: dict) -> str:
    title = p.get("title") or "제목 미상"
    parts = [title]
    artist = p.get("artist_name")
    if artist:
        parts.append(f" — {artist}")
    rel = p.get("release_year")
    tail = "는 평가 대상 음악저작물이다."
    if rel:
        tail = f"는 {rel} 발매된 평가 대상 음악저작물이다."
    wid = p.get("work_id")
    if wid:
        tail += f" 작품식별자 {wid}."
    return "".join(parts) + tail


def card_for_artist(p: dict) -> str:
    name = p.get("name") or "아티스트 미상"
    role = p.get("role")
    role_s = f" ({role})" if role else ""
    debut = p.get("debut_year")
    debut_s = f" {debut} 데뷔." if debut else ""
    return f"{name}{role_s} — 아티스트.{debut_s}"


def card_for_rightsholder(p: dict) -> str:
    name = p.get("name") or "권리자 미상"
    kind = p.get("kind", "")  # 퍼블리셔/레이블/저작권신탁 등
    kind_s = f" ({kind})" if kind else ""
    share = p.get("share")
    share_s = f" 지분 {share}%." if isinstance(share, (int, float)) else ""
    return f"{name}{kind_s} — 권리자.{share_s}"


def card_for_platform(p: dict) -> str:
    # 지표는 의미가 서로 다르다 — playcount(재생수)·listeners(청취자)·rank(인기지수,
    # 0~1M)·popularity(0~100). 한 필드로 뭉치면 'rank를 재생수로' 같은 사실오류가
    # 보고서로 전파되므로 각각 정확히 표기한다.
    name = p.get("name") or "플랫폼 미상"
    bits = []
    pc = _fmt_int(p.get("playcount"))
    if pc:
        bits.append(f"누적 재생 {pc}회")
    li = _fmt_int(p.get("listeners"))
    if li:
        bits.append(f"청취자 {li}명")
    rk = _fmt_int(p.get("rank"))
    if rk:
        bits.append(f"플랫폼 인기지수 {rk}")
    pop = p.get("popularity")
    if isinstance(pop, (int, float)):
        bits.append(f"인기도 {pop}/100")
    tail = (" " + ", ".join(bits) + ".") if bits else ""
    return f"{name} — 유통 플랫폼.{tail}"


def card_for_chart(p: dict) -> str:
    name = p.get("chart_name", "차트 미상")
    peak = p.get("peak_rank")
    peak_s = f" 최고 {peak}위." if peak else ""
    weeks = p.get("weeks_on")
    weeks_s = f" {weeks}주 차트인." if weeks else ""
    region = p.get("region", "")
    region_s = f"({region}) " if region else ""
    return f"{region_s}{name} 차트 기록.{peak_s}{weeks_s}"


def card_for_award(p: dict) -> str:
    name = p.get("award_name", "수상 미상")
    cat = p.get("category", "")
    year = p.get("year", "")
    result = p.get("result", "수상")  # 수상/노미네이트
    cat_s = f" {cat}" if cat else ""
    year_s = f"{year} " if year else ""
    return f"{year_s}{name}{cat_s} — {result}."


# ── 확장 수집물 (Harvester) — real_estate와 동형 ──
def card_for_web_doc(p: dict) -> str:
    title = p.get("title", "제목 미상")
    press = p.get("press") or p.get("source") or ""
    date = (p.get("published_at") or "")[:10]
    meta = ", ".join(x for x in (press, date) if x)
    return f"문서: {title}" + (f" ({meta})." if meta else ".")


def card_for_fact(p: dict) -> str:
    text = p.get("text", "")
    date = (p.get("published_at") or "")[:10]
    return f"{text}" + (f" ({date} 보도)" if date else "")


def card_for_entity(p: dict) -> str:
    name = p.get("name") or p.get("entity_id", "엔티티 미상")
    guess = p.get("guess_label")
    suffix = f" (추정 종류: {guess})" if guess else ""
    return f"{name} — 수집 문서에서 언급된 엔티티{suffix}."


_BUILDERS = {
    NODE_LABELS.WORK: card_for_work,
    NODE_LABELS.ARTIST: card_for_artist,
    NODE_LABELS.RIGHTSHOLDER: card_for_rightsholder,
    NODE_LABELS.PLATFORM: card_for_platform,
    NODE_LABELS.CHART: card_for_chart,
    NODE_LABELS.AWARD: card_for_award,
    NODE_LABELS.WEB_DOC: card_for_web_doc,
    NODE_LABELS.FACT: card_for_fact,
    NODE_LABELS.ENTITY: card_for_entity,
}


def build_card(label: str, props: dict) -> str:
    """라벨+프로퍼티로 카드 텍스트 생성. 빌더 미정의 라벨은 빈 문자열.

    음악 빌더 MAP(_BUILDERS)을 infra 레지스트리 메커니즘에 주입한다(real_estate와
    동일 — infra 코드 복제 없음).
    """
    from infra.graph.cards import build_card as _build_card

    return _build_card(label, props, _BUILDERS)
