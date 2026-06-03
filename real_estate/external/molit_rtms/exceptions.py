"""국토교통부 RTMS(실거래가) OpenAPI 호출 관련 예외."""


class MolitRtmsError(Exception):
    """MOLIT RTMS API 호출/응답 처리 실패."""

    def __init__(
        self,
        message,
        *,
        api_name=None,
        endpoint=None,
        result_code=None,
        status=None,
    ):
        super().__init__(message)
        self.api_name = api_name
        self.endpoint = endpoint
        self.result_code = result_code
        self.status = status
