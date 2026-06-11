"""ValidityCheck — "이 소스가 지금도 유효한가"의 공용 판단 로직 (설계 §12, D10).

Composer(보고서)·QA·Auditor·Navigator·Reviser가 전부 이 모듈 하나를 쓴다.

판단 순서:
    ① 명시 유효기간(valid_until)이 있으면 그것이 우선 — 지났으면 EXPIRED(0.1)
    ② 정적 사실(temporal_kind='static', 또는 시간 속성 없는 구조 노드)은 무감쇠
    ③ 폴백: 반감기 감쇠 decay = 0.5 ^ (경과개월 / 반감기)
       뉴스 Fact 6개월 / 분기 통계·문서 12개월

오래된/만료 노드는 삭제하지 않는다 — walk에서 후순위로 밀리고, Navigator
frontier에서 재검색 신호로 기능한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

logger = logging.getLogger("graphflow.validity")

# 라벨 종류별 반감기 (개월). 미등록 라벨은 시간 속성이 있을 때만 DEFAULT 적용.
HALF_LIFE_MONTHS = {
    "Fact": 6.0,
    "WebDoc": 6.0,
    "UserDoc": 12.0,
}
_DEFAULT_HALF_LIFE = 12.0

_EXPIRED_SCORE = 0.1


@dataclass(frozen=True)
class Validity:
    status: str          # VALID | EXPIRED | DECAYED
    score: float         # 0~1 — walk 경로 점수에 곱해지는 배수


VALID = Validity("VALID", 1.0)


def _parse_date(value) -> date | None:
    """ISO 유사 문자열 → date. 'YYYY', 'YYYY-MM', 'YYYY-MM-DD', ISO datetime 수용."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    try:
        if len(v) == 4 and v.isdigit():                  # YYYY → 연말로
            return date(int(v), 12, 31)
        if len(v) == 7:                                   # YYYY-MM → 월말 근사
            y, m = int(v[:4]), int(v[5:7])
            return date(y, m, 28)
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _months_between(earlier: date, later: date) -> float:
    return max(0.0, (later - earlier).days / 30.44)


def validity(props: dict, *, label: str = "", as_of: date | None = None) -> Validity:
    """노드 프로퍼티 → 유효성 판정. props는 published_at/valid_until/temporal_kind 사용.

    구조 노드(시간 속성 없음)는 VALID(1.0) — 분기 통계 요약 프로퍼티의 신선도는
    적재 시점에 latest로 교체되므로 여기서 감쇠하지 않는다.
    """
    today = as_of or date.today()

    vu = _parse_date(props.get("valid_until"))
    if vu is not None:                                   # ① 명시 유효기간 우선
        return VALID if today <= vu else Validity("EXPIRED", _EXPIRED_SCORE)

    if props.get("temporal_kind") == "static":           # ② 정적 사실
        return VALID

    pub = _parse_date(props.get("published_at"))
    if pub is None:                                      # 시간 속성 없는 구조 노드
        return VALID

    half_life = HALF_LIFE_MONTHS.get(label, _DEFAULT_HALF_LIFE)   # ③ 반감기 폴백
    score = 0.5 ** (_months_between(pub, today) / half_life)
    return Validity("DECAYED", round(score, 4))
