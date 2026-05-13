"""Pure-function keyword/regex rule filter (stage 1 of pipeline)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from common.ports.repository import NormalizedNews


class Rule(BaseModel):
    name: str
    keywords: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class FilterResult(BaseModel):
    passed: bool
    matched: list[str] = Field(default_factory=list)


def apply(news: NormalizedNews, rules: list[Rule]) -> FilterResult:
    raise NotImplementedError
