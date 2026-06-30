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
    card = "".join(parts) + tail
    # 음차 별칭(다국어 표기) — grounding 파서(_work_aliases_from_card)가 읽어 영문/원어 어느
    # 표기든 같은 작품으로 인정한다. 형식 고정: "[별칭] 제목: a|b · 아티스트: x|y".
    talias = [a for a in (p.get("title_aliases") or []) if a]
    aalias = [a for a in (p.get("artist_aliases") or []) if a]
    if talias or aalias:
        card += (f" [별칭] 제목: {'|'.join(talias) or '-'}"
                 f" · 아티스트: {'|'.join(aalias) or '-'}")
    return card


def card_for_release(p: dict) -> str:
    title = p.get("title") or p.get("release_title") or p.get("name") or "발매물 미상"
    kind = p.get("kind") or "발매물"
    artist = p.get("artist_name")
    year = p.get("release_year") or p.get("year")
    bits = []
    if artist:
        bits.append(f"아티스트 {artist}")
    if year:
        bits.append(f"{year} 발매")
    tail = " " + ", ".join(bits) + "." if bits else ""
    return f"{title} ({kind}) — 앨범·싱글·EP 등 발매 단위.{tail}"


def card_for_artist(p: dict) -> str:
    name = p.get("name") or "아티스트 미상"
    role = p.get("role")
    role_s = f" ({role})" if role else ""
    debut = p.get("debut_year")
    debut_s = f" {debut} 데뷔." if debut else ""
    return f"{name}{role_s} — 아티스트.{debut_s}"


def card_for_genre(p: dict) -> str:
    name = p.get("name") or p.get("genre_name") or "장르 미상"
    desc = p.get("description") or ""
    desc_s = f" {desc}" if desc else ""
    return f"{name} — 음악 장르·스타일.{desc_s}"


def card_for_agency(p: dict) -> str:
    name = p.get("name") or p.get("agency_name") or "기획사 미상"
    kind = p.get("kind") or "기획사·매니지먼트"
    artist = p.get("artist_name")
    artist_s = f" {artist}의 소속·관리 조직." if artist else ""
    return f"{name} ({kind}) — 음악 아티스트의 기획·매니지먼트 조직.{artist_s}"


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


def card_for_media_event(p: dict) -> str:
    name = p.get("name") or p.get("event_name") or "미디어 이벤트 미상"
    kind = p.get("kind") or "방송·공연·행사"
    date = p.get("event_date") or p.get("year")
    date_s = f" ({date})" if date else ""
    desc = p.get("description") or ""
    desc_s = f" {desc}" if desc else ""
    return f"{name}{date_s} — {kind} 노출·공연 맥락.{desc_s}"


def card_for_viral_trend(p: dict) -> str:
    name = p.get("name") or p.get("trend_name") or "확산 현상 미상"
    platform = p.get("platform")
    plat_s = f"{platform} " if platform else ""
    desc = p.get("description") or ""
    desc_s = f" {desc}" if desc else ""
    return f"{plat_s}{name} — 챌린지·밈·해시태그 등 대중 확산 현상.{desc_s}"


def card_for_certification(p: dict) -> str:
    # 음반 인증(RIAA/Gaon 등) — 지속성·대중성 신호. 등급(Gold/Platinum/Diamond)과
    # 인증기관(body)·지역(region)·연도를 정확히 표기한다.
    body = p.get("body", "")
    level = p.get("level", "인증")
    region = p.get("region", "")
    year = p.get("year", "")
    body_s = f"{body} " if body else ""
    region_s = f"({region}) " if region else ""
    year_s = f" {year}" if year else ""
    return f"{region_s}{body_s}{level} 인증{year_s}."


def card_for_social_metric(p: dict) -> str:
    # 소셜/스트리밍 지표 스냅샷 — 플랫폼별 의미가 달라 metric 이름을 그대로 표기
    # (followers/monthly_listeners/views/tiktok_uses 등). as_of로 시점 명시.
    platform = p.get("platform", "")
    metric = p.get("metric", "지표")
    value = _fmt_int(p.get("value"))
    as_of = p.get("as_of", "")
    plat_s = f"{platform} " if platform else ""
    val_s = f" {value}" if value else ""
    as_of_s = f" (기준 {as_of})" if as_of else ""
    video = p.get("video_title")
    channel = p.get("channel")
    target_s = f" '{video}'" if video else ""
    channel_s = f" / 채널 {channel}" if channel else ""
    return f"{plat_s}{metric}{val_s}{target_s}{channel_s}{as_of_s}."


def card_for_fandom(p: dict) -> str:
    name = p.get("name") or p.get("fandom_name") or "팬덤 미상"
    artist = p.get("artist_name")
    artist_s = f"{artist}의 " if artist else ""
    desc = p.get("description") or ""
    desc_s = f" {desc}" if desc else ""
    return f"{name} — {artist_s}팬덤 공동체.{desc_s}"


# ── 사회적 가치 신호 (가사·메시지·운동·공익 활용) ──
def card_for_social_theme(p: dict) -> str:
    # 작품이 다루는 사회적 의제(정의·인권·정신건강·환경·정체성 등). 카드에 곡 맥락을
    # 박아 '무관한 의제'와 '이 곡의 의제'가 의미검색에서 변별되게 한다.
    theme = p.get("theme") or p.get("label") or "사회적 의제"
    title = p.get("work_title")
    subj = f"'{title}'이(가) 다루는" if title else "이 작품이 다루는"
    desc = p.get("description") or ""
    desc_s = f" {desc}" if desc else ""
    return f"사회적 의제 '{theme}' — {subj} 사회·문화적 주제.{desc_s}"


def card_for_cause(p: dict) -> str:
    # 연계된 사회운동·시위·공익/자선 캠페인 (앤섬화·기부·연대 등).
    name = p.get("name") or "사회운동·캠페인"
    kind = p.get("kind") or ""  # 사회운동/시위/공익캠페인/자선·기부 등
    kind_s = f" ({kind})" if kind else ""
    desc = p.get("description") or ""
    desc_s = f" {desc}" if desc else ""
    return f"{name}{kind_s} — 이 작품과 연계된 사회운동·공익 활동.{desc_s}"


def card_for_public_use(p: dict) -> str:
    # 공익·교육·기념·문화외교 등 공적 활용 사례.
    context = p.get("context") or p.get("label") or "공적 활용"
    kind = p.get("kind") or ""  # 교육/기념식/시위/공공행사/문화외교 등
    kind_s = f" ({kind})" if kind else ""
    desc = p.get("description") or ""
    desc_s = f" {desc}" if desc else ""
    return f"{context}{kind_s} — 이 작품의 공익·공적 활용 사례.{desc_s}"


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
    excerpt = (p.get("source_excerpt") or "").strip()
    suffix = f" ({date} 보도)" if date else ""
    if excerpt:
        suffix += f" 근거 원문: {excerpt[:220]}"
    return f"{text}{suffix}"


def card_for_entity(p: dict) -> str:
    name = p.get("name") or p.get("entity_id", "엔티티 미상")
    guess = p.get("guess_label")
    suffix = f" (추정 종류: {guess})" if guess else ""
    return f"{name} — 수집 문서에서 언급된 엔티티{suffix}."


# ── 보고서 grounding (real_estate와 동형 — 보고서 노드도 카드를 갖는 1급 시민) ──
def card_for_report(p: dict) -> str:
    return f"평가 보고서 {p.get('report_id', '')} ({(p.get('created_at') or '')[:10]})."


def card_for_report_section(p: dict) -> str:
    return f"보고서 섹션 [{p.get('axis', '')}] {p.get('title', '')}"


def card_for_claim(p: dict) -> str:
    # 카드 = 주장 문장 자체 (QA 앵커 매칭에서 보고서 결론이 잡히도록)
    text = p.get("text", "")
    as_of = f" (기준 {p['as_of']})" if p.get("as_of") else ""
    return f"{text}{as_of}"


_BUILDERS = {
    NODE_LABELS.WORK: card_for_work,
    NODE_LABELS.RELEASE: card_for_release,
    NODE_LABELS.ARTIST: card_for_artist,
    NODE_LABELS.GENRE: card_for_genre,
    NODE_LABELS.AGENCY: card_for_agency,
    NODE_LABELS.RIGHTSHOLDER: card_for_rightsholder,
    NODE_LABELS.PLATFORM: card_for_platform,
    NODE_LABELS.CHART: card_for_chart,
    NODE_LABELS.AWARD: card_for_award,
    NODE_LABELS.MEDIA_EVENT: card_for_media_event,
    NODE_LABELS.VIRAL_TREND: card_for_viral_trend,
    NODE_LABELS.CERTIFICATION: card_for_certification,
    NODE_LABELS.SOCIAL_METRIC: card_for_social_metric,
    NODE_LABELS.FANDOM: card_for_fandom,
    NODE_LABELS.SOCIAL_THEME: card_for_social_theme,
    NODE_LABELS.CAUSE: card_for_cause,
    NODE_LABELS.PUBLIC_USE: card_for_public_use,
    NODE_LABELS.WEB_DOC: card_for_web_doc,
    NODE_LABELS.FACT: card_for_fact,
    NODE_LABELS.ENTITY: card_for_entity,
    NODE_LABELS.REPORT: card_for_report,
    NODE_LABELS.REPORT_SECTION: card_for_report_section,
    NODE_LABELS.CLAIM: card_for_claim,
}


def build_card(label: str, props: dict) -> str:
    """라벨+프로퍼티로 카드 텍스트 생성. 빌더 미정의 라벨은 빈 문자열.

    음악 빌더 MAP(_BUILDERS)을 infra 레지스트리 메커니즘에 주입한다(real_estate와
    동일 — infra 코드 복제 없음).
    """
    from infra.graph.cards import build_card as _build_card

    return _build_card(label, props, _BUILDERS)
