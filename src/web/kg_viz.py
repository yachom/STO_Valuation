"""뉴스 지식그래프 인터랙티브 시각화 (pyvis 기반).

QASession의 KnowledgeGraph(networkx.MultiDiGraph)를 받아 pyvis Network로 변환,
임시 HTML을 만들어 Streamlit에 임베드한다.

축 필터:
  - "ALL": 전체 그래프
  - "ESG" | "AWARENESS" | "VITALITY":
      해당 Axis 노드 + Asset → Axis 엣지 + 그 엣지에 연결된 NewsArticle +
      해당 article에서 MENTIONED_IN으로 묶인 엔티티만 부분 그래프로 추출.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import networkx as nx


_TYPE_COLOR = {
    "Asset": "#e74c3c",         # red
    "Axis": "#9b59b6",          # purple
    "NewsArticle": "#3498db",   # blue
    "Person": "#1abc9c",
    "Organization": "#16a085",
    "Company": "#16a085",
    "Place": "#f39c12",
    "Location": "#f39c12",
    "Event": "#e67e22",
    "Issue": "#e67e22",
    "Policy": "#2ecc71",
    "Brand": "#f1c40f",
}
_DEFAULT_COLOR = "#95a5a6"


def _node_color(node_type: str | None) -> str:
    if not node_type:
        return _DEFAULT_COLOR
    return _TYPE_COLOR.get(node_type, _DEFAULT_COLOR)


def _node_label(data: dict[str, Any]) -> str:
    name = data.get("name") or "(unnamed)"
    return name if len(name) <= 24 else name[:22] + "…"


def _node_tooltip(data: dict[str, Any]) -> str:
    parts = [f"<b>{data.get('name', '')}</b>", f"<i>{data.get('type', '')}</i>"]
    for key in ("link", "pubDate", "query"):
        if data.get(key):
            parts.append(f"{key}: {data.get(key)}")
    return "<br/>".join(parts)


def filter_subgraph(g: nx.MultiDiGraph, axis: str) -> nx.MultiDiGraph:
    """축별 부분 그래프 추출. axis='ALL'이면 원본 반환."""
    if axis == "ALL":
        return g

    keep: set[str] = set()
    axis_node = None
    for nid, data in g.nodes(data=True):
        if data.get("type") == "Axis" and (data.get("name") or "").upper() == axis.upper():
            axis_node = nid
            break
    if axis_node is None:
        return g.subgraph([]).copy()

    keep.add(axis_node)
    article_ids: set[str] = set()
    for u, v, _, data in g.in_edges(axis_node, keys=True, data=True):
        if data.get("rel") == "AFFECTS_AXIS":
            keep.add(u)  # Asset
            art = data.get("article")
            if art:
                article_ids.add(art)
    keep.update(article_ids)

    for art_id in article_ids:
        for u, _, _, data in g.in_edges(art_id, keys=True, data=True):
            if data.get("rel") == "MENTIONED_IN":
                keep.add(u)

    return g.subgraph(keep).copy()


def cap_by_degree(g: nx.MultiDiGraph, max_nodes: int) -> nx.MultiDiGraph:
    """노드가 많을 때 degree 상위 N개만 남긴다 (Asset/Axis는 항상 포함)."""
    if g.number_of_nodes() <= max_nodes:
        return g
    pinned = {nid for nid, data in g.nodes(data=True) if data.get("type") in ("Asset", "Axis")}
    rest = [nid for nid in g.nodes() if nid not in pinned]
    rest.sort(key=lambda n: g.degree(n), reverse=True)
    keep = pinned | set(rest[: max(0, max_nodes - len(pinned))])
    return g.subgraph(keep).copy()


def render_html(g: nx.MultiDiGraph, height_px: int = 650) -> str:
    """networkx 그래프를 pyvis로 렌더링해 HTML 문자열 반환."""
    from pyvis.network import Network

    net = Network(
        height=f"{height_px}px",
        width="100%",
        directed=True,
        bgcolor="#ffffff",
        font_color="#222",
        cdn_resources="remote",
        notebook=False,
    )
    net.barnes_hut(gravity=-2500, central_gravity=0.2, spring_length=120, spring_strength=0.02)

    for nid, data in g.nodes(data=True):
        ntype = data.get("type")
        net.add_node(
            nid,
            label=_node_label(data),
            title=_node_tooltip(data),
            color=_node_color(ntype),
            shape="dot" if ntype != "NewsArticle" else "square",
            size=22 if ntype in ("Asset", "Axis") else 14,
        )

    seen_edges: set[tuple[str, str, str]] = set()
    for u, v, _, data in g.edges(keys=True, data=True):
        rel = data.get("rel") or "REL"
        sig = (u, v, rel)
        if sig in seen_edges:
            continue
        seen_edges.add(sig)
        tooltip_parts = [f"<b>{rel}</b>"]
        for k in ("polarity", "strength", "note", "evidence"):
            if data.get(k):
                tooltip_parts.append(f"{k}: {str(data.get(k))[:160]}")
        edge_color = "#c0392b" if data.get("polarity") == "negative" else (
            "#27ae60" if data.get("polarity") == "positive" else "#7f8c8d"
        )
        net.add_edge(u, v, label=rel, title="<br/>".join(tooltip_parts), color=edge_color, arrows="to")

    tmp = Path(tempfile.mkdtemp()) / "kg.html"
    net.write_html(str(tmp), open_browser=False, notebook=False)
    html = tmp.read_text(encoding="utf-8")
    try:
        tmp.unlink()
        tmp.parent.rmdir()
    except OSError:
        pass
    return html
