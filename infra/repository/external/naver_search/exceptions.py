"""Naver Open API 호출 관련 예외.

Naver의 여러 Open API가 errorCode 체계를 공유하므로 search 전용으로
이름 짓지 않는다. search 고유 에러가 진짜로 나타날 때 NaverSearchAPIError 같은
하위 클래스를 추가하면 된다.

호출 측은 NaverAPIError 만 잡아도 되고, 세분화된 처리가 필요하면
하위 클래스(인증/쿼터/레이트 등) 를 개별적으로 잡으면 된다.
"""


class NaverAPIError(Exception):
    """Naver Open API 호출 실패 공통 베이스."""

    def __init__(self, message, *, status=None, error_code=None, response_body=None):
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.response_body = response_body

    def __str__(self):
        base = super().__str__()
        meta = []
        if self.status is not None:
            meta.append(f"status={self.status}")
        if self.error_code:
            meta.append(f"errorCode={self.error_code}")
        return f"{base} ({', '.join(meta)})" if meta else base


class NaverAuthError(NaverAPIError):
    """클라이언트 ID/Secret 누락·오류 (HTTP 401, errorCode SE01/024 등)."""


class NaverQuotaExceeded(NaverAPIError):
    """일일/월간 호출 한도 초과 (errorCode 024, 일부 010 케이스)."""


class NaverRateLimited(NaverAPIError):
    """초당 호출 한도 초과 (HTTP 429)."""


class NaverBadRequest(NaverAPIError):
    """파라미터 오류 (HTTP 400)."""
