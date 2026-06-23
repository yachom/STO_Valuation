"""ddgs 호출 예외 — ddgs 자체 예외(DDGSException 계층)를 우리 타입으로 래핑.

호출 측은 DdgsError 만 잡아도 되고, 레이트리밋/타임아웃을 구분하려면 하위
클래스를 잡으면 된다. Tavily(TavilyError)와 같은 RuntimeError 계열.
"""


class DdgsError(RuntimeError):
    """ddgs 호출 실패 공통 베이스."""

    def __init__(self, message, *, cause=None):
        super().__init__(message)
        self.cause = cause


class DdgsRateLimited(DdgsError):
    """레이트리밋 초과 (ddgs RatelimitException). backend='auto'+게이트웨이로 완충."""


class DdgsTimeout(DdgsError):
    """타임아웃 (ddgs TimeoutException)."""
