"""건축HUB API 호출 관련 예외."""


class BuildingHubError(Exception):
    """건축HUB API 호출/응답 처리 실패."""

    def __init__(self, message, *, endpoint=None, result_code=None, status=None):
        super().__init__(message)
        self.endpoint = endpoint
        self.result_code = result_code
        self.status = status
