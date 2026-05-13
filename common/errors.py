"""Common exception hierarchy."""

from __future__ import annotations


class STOError(Exception):
    """Base for all project errors."""


class ConfigError(STOError):
    pass


class ExternalAPIError(STOError):
    pass


class RateLimitError(ExternalAPIError):
    pass


class QuotaExceededError(ExternalAPIError):
    pass


class LLMError(STOError):
    pass


class PolicyViolation(STOError):
    pass


class RepositoryError(STOError):
    pass
