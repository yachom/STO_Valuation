"""Naver Open API 자격증명.

Naver의 여러 Open API(search/datalab/papago 등)는 같은 dev console의
NAVER_CLIENT_ID/NAVER_CLIENT_SECRET을 공유한다. 이 파일은 search에 종속되지
않으며, 추후 다른 Naver API client가 추가되면 그쪽에서도 그대로 import해 쓴다.

.env 로딩은 이 모듈의 책임이 아니다.
entrypoint(scripts/*, main.py)에서 infra.env.load_env()를 1회 호출한 뒤,
이 모듈은 os.environ에서만 값을 읽는다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from infra.external.naver_search.exceptions import NaverAuthError

# Naver client id/secret 의 형식은 공개돼 있지 않지만,
# 실제 발급값은 영문자/숫자 조합이며 길이가 안정적이라 형식 sanity check만 둔다.
_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")
_CLIENT_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")


@dataclass(frozen=True)
class NaverCredentials:
    client_id: str
    client_secret: str

    def as_headers(self) -> dict:
        return {
            "X-Naver-Client-Id": self.client_id,
            "X-Naver-Client-Secret": self.client_secret,
        }

    def masked(self) -> dict:
        return {
            "NAVER_CLIENT_ID": self.client_id,
            "NAVER_CLIENT_SECRET": "***masked***",
        }


def load_credentials() -> NaverCredentials:
    """os.environ에서 NAVER_CLIENT_ID/SECRET을 읽어 검증 후 반환.

    .env 로딩은 entrypoint에서 infra.env.load_env()로 미리 수행되어 있어야 한다.
    실패 시 NaverAuthError를 던진다.
    """
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")

    missing = [
        name
        for name, val in (
            ("NAVER_CLIENT_ID", client_id),
            ("NAVER_CLIENT_SECRET", client_secret),
        )
        if not val
    ]
    if missing:
        raise NaverAuthError(
            f"필수 환경변수 누락: {', '.join(missing)}. "
            ".env 또는 프로세스 환경변수에 설정하세요."
        )

    if not _CLIENT_ID_PATTERN.match(client_id):
        raise NaverAuthError("NAVER_CLIENT_ID 형식이 비정상입니다 (영문/숫자 8~64자).")
    if not _CLIENT_SECRET_PATTERN.match(client_secret):
        raise NaverAuthError("NAVER_CLIENT_SECRET 형식이 비정상입니다 (영문/숫자 8~64자).")

    return NaverCredentials(client_id=client_id, client_secret=client_secret)
