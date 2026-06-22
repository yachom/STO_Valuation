"""주소 파싱·정제용 공유 정규식 + 문자열 헬퍼.

efficiency_pipeline.py / gseed_pipeline.py가 동일하게 쓰던 정규식과 _clean을
한 곳에 모은다. 패턴 문자열은 기존 두 파일과 바이트 단위로 동일하다.

두 종류의 (구, 동) 정규식이 있다 — 회수 단계마다 미묘하게 다르므로 둘 다 보존한다:
    JIBUN_GU_DONG_RE / JIBUN_GU_RE   JibunDeriver용 ("서울"이 앞에 필수)
    NAME_GU_DONG_RE  / NAME_GU_RE    NameLookup용  ("서울"이 선택적)
    GU_DONG_PAREN_RE                  괄호 안 동 (예: "...구 (역삼동)")
"""

import re

# 공백 정규화
WS_RE = re.compile(r"\s+")

# JibunDeriver: "서울"이 앞에 와야 함
JIBUN_GU_DONG_RE = re.compile(r"서울\S*\s+(\S+구)\s+([가-힣]+[가-힣\d]*(?:동|가|리)\S*)")
JIBUN_GU_RE = re.compile(r"서울\S*\s+(\S+구)")
GU_DONG_PAREN_RE = re.compile(r"(\S+구)\b[^()]*\(([가-힣]+\d*가?)\)")

# NameLookup: "서울"이 선택적 (juso 결과는 시도 없이 구부터 올 수 있음)
NAME_GU_DONG_RE = re.compile(r"(?:서울\S*\s*)?(\S+구)\s+([가-힣]+[가-힣\d]*(?:동|가|리)\S*)")
NAME_GU_RE = re.compile(r"(?:서울\S*\s*)?(\S+구)")


def clean(text):
    """문자열 strip + 다중 공백 단일화. None 안전."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    return WS_RE.sub(" ", s)
