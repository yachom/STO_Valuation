"""공공데이터포털(data.go.kr) API 호출 관련 예외."""


class PublicDataPortalError(Exception):
    """공공데이터포털 API 호출/응답 처리 실패."""

    def __init__(self, message, *, api_name=None, endpoint=None, result_code=None, status=None):
        super().__init__(message)
        self.api_name = api_name
        self.endpoint = endpoint
        self.result_code = result_code
        self.status = status
