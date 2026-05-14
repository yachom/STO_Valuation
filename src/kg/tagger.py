"""뉴스 기사 1건을 받아 LLM으로 온톨로지 태깅."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..agents._llm import make_llm
from .ontology import SYSTEM_PROMPT


def _strip_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return m.group(1).strip() if m else text


def tag_article(asset_context: dict[str, Any], article: dict[str, Any], callbacks: list[Any] | None = None) -> dict[str, Any]:
    """asset_context는 {asset_name, road, jibun, sigungu, bjdong} 정도."""
    llm = make_llm(max_tokens=2000)
    body = (
        f"# 대상 자산\n```json\n{json.dumps(asset_context, ensure_ascii=False)}\n```\n\n"
        f"# 기사\n제목: {article.get('title')}\n"
        f"요약: {article.get('description')}\n"
        f"링크: {article.get('link')}\n"
        f"발행: {article.get('pubDate')}\n"
        f"검색어: {article.get('query')}\n"
    )
    resp = llm.invoke(
        [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=body)],
        config={"callbacks": callbacks or []},
    )
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    text = _strip_fence(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return {"asset_relevance": "none", "entities": [], "relations": [], "axis_signals": [], "parse_error": text[:500]}


def tag_articles(asset_context: dict[str, Any], articles: list[dict[str, Any]], callbacks: list[Any] | None = None, max_articles: int = 30) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for art in articles[:max_articles]:
        try:
            tagged = tag_article(asset_context, art, callbacks=callbacks)
        except Exception as exc:
            tagged = {"asset_relevance": "none", "error": f"{type(exc).__name__}: {exc}"}
        results.append({"article": art, "tagged": tagged})
    return results
