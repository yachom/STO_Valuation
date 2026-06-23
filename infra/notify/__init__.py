"""알림 어댑터 — 운영 신호를 외부 채널(Slack 등)로 내보내는 best-effort 레이어.

`external/`(데이터 수집 어댑터)와 달리, 여기는 **관측/운영 통지** 전용이다.
파이프라인은 이 레이어가 실패해도 절대 멈추지 않는다(모든 예외 swallow).

공개 API:
    from infra.notify import SlackNotifier, enable_slack_alerting, get_notifier
"""

from infra.notify.slack import (
    SlackNotifier,
    enable_slack_alerting,
    disable_slack_alerting,
    get_notifier,
    classify_channel,
)

__all__ = [
    "SlackNotifier",
    "enable_slack_alerting",
    "disable_slack_alerting",
    "get_notifier",
    "classify_channel",
]
