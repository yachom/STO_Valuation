"""검색 응답 텍스트 정규화 공통 헬퍼.

news_search.py 와 vertical_search.py 가 동일하게 쓰던 HTML 태그 제거·
pubDate ISO 변환을 한 곳으로 모은다. Naver search family 의 모든 vertical 이
title/description 에 `<b>...</b>` 강조 태그와 RFC822 형식 날짜를 동일하게
돌려주므로, vertical 모듈 2개 이상이 관찰된 시점(news + blog/cafe/web/kin)에
공통 base 로 추출한다. transport/도메인 의미는 담지 않는 순수 텍스트 유틸이다.
"""

from __future__ import annotations

import html
import re
from email.utils import parsedate_to_datetime

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """`<b>` 강조 태그 등 HTML 제거 + 엔티티 unescape + trim."""
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def normalize_pub_date(value: str) -> str:
    """RFC822(`Mon, 01 Jan 2024 ...`) → ISO 8601. 파싱 실패 시 원문 유지."""
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value
