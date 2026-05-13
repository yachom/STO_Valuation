"""LLM registry — name → instance, loaded from per-asset YAML config.

YAML shape (projects/<asset>/config/llm.yaml):

    planner:    {provider: anthropic, model: claude-opus-4-7,    temperature: 0.1}
    retriever:  {provider: openai,    model: gpt-4o-mini,        temperature: 0.0}
    critic:     {provider: anthropic, model: claude-sonnet-4-6,  temperature: 0.0}
    tagger:     {provider: openai,    model: gpt-4o-mini,        temperature: 0.0}

Env vars LLM_<ROLE> (e.g. LLM_PLANNER) override the YAML if set.
"""

from __future__ import annotations

from pathlib import Path

from typing import Any

from common.ports.llm import LLMClient


class LLMRegistry:
    def __init__(self, role_to_client: dict[str, LLMClient]) -> None:
        self._roles = role_to_client

    def get(self, role: str) -> LLMClient:
        raise NotImplementedError

    def __getitem__(self, role: str) -> LLMClient:
        return self.get(role)


def load_registry(settings: Any, yaml_path: str | Path) -> LLMRegistry:
    """Read the YAML, instantiate provider adapters, apply env overrides."""
    raise NotImplementedError
