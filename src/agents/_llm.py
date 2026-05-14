"""Shared LLM helpers."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from .. import config


def make_llm(model: str | None = None, max_tokens: int = 4096, temperature: float | None = 0.2) -> ChatAnthropic:
    chosen = model or config.AGENT_MODEL
    # Opus 4.7 (and newer reasoning-style models) reject the temperature param.
    kwargs: dict[str, Any] = {
        "model": chosen,
        "api_key": config.ANTHROPIC_API_KEY,
        "max_tokens": max_tokens,
    }
    if temperature is not None and "opus-4-7" not in chosen:
        kwargs["temperature"] = temperature
    return ChatAnthropic(**kwargs)


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def score_with_llm(
    system_prompt: str,
    payload: dict[str, Any],
    *,
    axis_label: str,
    model: str | None = None,
    callbacks: list[Any] | None = None,
) -> dict[str, Any]:
    """Ask LLM to return a JSON scoring dict for an axis."""
    llm = make_llm(model=model)
    body = (
        f"# 평가 축: {axis_label}\n\n"
        "아래 수집 데이터에 근거해 평가하라.\n"
        "출력은 반드시 다음 JSON 스키마로만 하라 (다른 텍스트 금지):\n"
        "{\n"
        '  "score": 0~100 숫자,\n'
        '  "grade": "A+|A|B+|B|C+|C|D",\n'
        '  "strengths": ["..."],\n'
        '  "weaknesses": ["..."],\n'
        '  "risks": ["..."],\n'
        '  "rationale": "근거 요약 (한국어, 5~10문장)"\n'
        "}\n\n"
        f"# 데이터\n```json\n{json.dumps(payload, ensure_ascii=False, default=str)[:60000]}\n```\n"
    )

    response = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=body)],
        config={"callbacks": callbacks or []},
    )
    text = response.content if isinstance(response.content, str) else str(response.content)
    text = _strip_json_fence(text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to recover the first JSON object
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {
            "score": None,
            "grade": "N/A",
            "strengths": [],
            "weaknesses": [],
            "risks": ["LLM 응답 파싱 실패"],
            "rationale": text[:2000],
        }
